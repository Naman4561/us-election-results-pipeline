"""Synthetic scale/recovery checks; synthetic counts never enter production."""

import copy
import hashlib
import io
import json
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import MagicMock, patch

import test_mississippi as fixtures
from pypdf import PdfWriter

from election_pipeline.checks import validate_payload
from election_pipeline.collectors import county_files
from election_pipeline.county_import import import_county, write_template
from election_pipeline.database import import_fetches
from election_pipeline.deployment import catalog, containers
from election_pipeline.parsers import ms
from election_pipeline.parsers.county_reports import reviewed_file
from election_pipeline.pipeline import collect_source
from election_pipeline.storage import ArchiveStore


def synthetic_review(fixture, key):
    """Blank image-like PDF plus explicit synthetic review, unique to one county."""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "SYNTHETIC TEST ONLY " + key})
    target = io.BytesIO()
    writer.write(target)
    raw = target.getvalue()
    review = dict(
        state="MS",
        county=key,
        election_date="2026-11-03",
        contest_id="MS-SEN",
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        source_url="https://example.com/official/" + key + ".pdf",
        official_page_url="https://example.com/official/" + key,
        observed_at="2026-11-04T18:00:00+00:00",
        reviewed_by="SYNTHETIC TEST ONLY",
        countywide_report_confirmed=True,
        report_generated_at=None,
        report_date="2026-11-03",
        report_time_note=None,
        counts=dict(zip(fixture.policy["candidates"], (10, 20, 3))),
        write_in_votes=None,
        write_in_label=None,
        reported_candidate_vote_total=None,
        precincts_reporting=None,
        precincts_total=None,
        absentee_scope="unspecified",
        affidavit_scope="unspecified",
    )
    return raw, review


def all_imports(fixture):
    admitted = {}
    for county in fixture.policy["counties"]:
        if county.get("url"):
            continue
        raw, review = synthetic_review(fixture, county["key"])
        record = dict(
            status="stale",
            url=review["source_url"],
            raw_sha256=review["raw_sha256"],
            issue="ManualSnapshot",
            headers={},
            last_success_at=review["observed_at"],
            last_attempt_at=review["observed_at"],
            review=review,
        )
        admitted[county["key"]] = (
            county,
            record,
            reviewed_file(raw, county, fixture.policy, review),
            raw,
        )
    return admitted


class CountyScalingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MississippiTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_additive_scan_review_can_reparse_an_approved_cached_bundle(self):
        f = self.f
        original, _ = f.collect()
        approved = f.policy["compatible_policy_sha256"][-1]
        cached = f.rewrite_bundle(
            original, lambda members, manifest: manifest.update(policy_sha256=approved)
        )
        self.assertIn("MADISON", ms.read_bundle(cached, f.source)[2])
        unknown = f.rewrite_bundle(
            original, lambda members, manifest: manifest.update(policy_sha256="0" * 64)
        )
        with self.assertRaisesRegex(ValueError, "unapproved policy"):
            ms.read_bundle(unknown, f.source)

    def test_82_counties_83_members_reconcile_without_claiming_live_complete_coverage(self):
        f = self.f
        original, _ = f.collect()
        target = f.store.temporary_download()
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("offline HTTP")):
            fetched = county_files.fetch_bundle(
                f.source,
                f.settings,
                target,
                previous=original,
                imports=all_imports(f),
                offline=True,
            )
        validate_payload(target, f.source, f.settings)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(len(archive.namelist()), 83)
        saved, _ = f.store.commit_download(target, fetched.sha256)
        report = f.parse(saved)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual((report["counties_with_counts"], report["output_rows"]), (82, 249))
        payload, _ = f.payload(report)
        self.assertEqual(payload["county_collection"]["manual"], 69)
        self.assertFalse(payload["county_collection"]["complete"])
        self.assertEqual(len(payload["races"]["MS-SEN"]["counties"]), 82)
        self.assertEqual(f.settings.max_zip_members, 20)
        self.assertEqual(f.source.effective_settings(f.settings).max_zip_members, 100)

    def test_import_is_offline_idempotent_and_does_not_refresh_other_counties_or_poll_time(self):
        f = self.f
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher()):
            first = collect_source(f.source, f.settings, f.root, force_download=True)
        self.assertEqual(first["outcome"], "downloaded", first)
        state = f.store.read_state()
        _, before, _ = ms.read_bundle(f.store.cached_archive(state), f.source)
        raw, review = synthetic_review(f, "JACKSON")
        original, review_path = f.root / "jackson.pdf", f.root / "jackson.json"
        original.write_bytes(raw)
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("import HTTP")):
            result = import_county(f.source, f.settings, f.root, "JACKSON", original, review_path)
            repeat = import_county(f.source, f.settings, f.root, "JACKSON", original, review_path)
        self.assertEqual(result["outcome"], "manual_imported")
        self.assertEqual(repeat["outcome"], "already_imported")
        after_state = f.store.read_state()
        self.assertEqual(after_state["last_checked_at"], state["last_checked_at"])
        _, after, _ = ms.read_bundle(f.store.cached_archive(after_state), f.source)
        self.assertEqual(after["counties"]["HINDS"], before["counties"]["HINDS"])
        # Manual receipts use the same database loader as normal HTTP receipts.
        with patch("election_pipeline.database.register_snapshot", return_value=1):
            self.assertGreaterEqual(import_fetches(MagicMock(), f.store), 2)
        review["counts"]["Scott Colom"] += 1
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different review"):
            import_county(f.source, f.settings, f.root, "JACKSON", original, review_path)
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher(conditional=True)):
            collect_source(f.source, f.settings, f.root, force_download=False)
        final = ms.read_bundle(f.store.cached_archive(f.store.read_state()), f.source)[1]
        self.assertEqual(final["counties"]["JACKSON"], after["counties"]["JACKSON"])

    def test_refused_automatic_county_takes_a_reviewed_copy_until_its_feed_answers(self):
        f = self.f
        blocked = {"HANCOCK": 403}
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher(failures=blocked)):
            collect_source(f.source, f.settings, f.root, force_download=True)
        state = f.store.read_state()
        _, before, accepted = ms.read_bundle(f.store.cached_archive(state), f.source)
        self.assertEqual(before["counties"]["HANCOCK"]["status"], "unavailable")
        self.assertNotIn("HANCOCK", accepted)

        county = f.counties["HANCOCK"]
        raw = f.raw["HANCOCK"]
        original, template = f.root / "hancock.pdf", f.root / "hancock.json"
        original.write_bytes(raw)
        write_template(f.source, "HANCOCK", original, template)
        review = json.loads(template.read_text())
        self.assertEqual(review["source_url"], county["url"])
        review.update(
            official_page_url="https://www.hancockcounty.ms.gov/DocumentCenter/Index/164",
            observed_at="2026-11-08T19:30:00+00:00",
            reviewed_by="TEST REVIEWER",
            countywide_report_confirmed=True,
            report_generated_at="2026-11-03T21:31:00-06:00",
            report_date="2026-11-03",
            counts={"Scott Colom": 4538, "Cindy Hyde-Smith": 11441, "Ty Pinkins": 753},
            write_in_votes=28,
            write_in_label="Write-In Totals",
            precincts_reporting=26,
            precincts_total=26,
        )
        wrong = {**review, "source_url": "https://example.com/hancock.pdf"}
        (f.root / "wrong.json").write_text(json.dumps(wrong), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configured URL"):
            import_county(f.source, f.settings, f.root, "HANCOCK", original, f.root / "wrong.json")
        template.write_text(json.dumps(review), encoding="utf-8")
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("import HTTP")):
            result = import_county(f.source, f.settings, f.root, "HANCOCK", original, template)
        self.assertEqual(result["outcome"], "manual_imported", result)

        # The site still refuses the server: the reviewed copy and its labels stay.
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher(failures=blocked)):
            collect_source(f.source, f.settings, f.root, force_download=True)
        _, manifest, accepted = ms.read_bundle(
            f.store.cached_archive(f.store.read_state()), f.source
        )
        record = manifest["counties"]["HANCOCK"]
        self.assertEqual((record["status"], record["issue"]), ("stale", "ManualSnapshot"))
        self.assertEqual(record["last_success_at"], review["observed_at"])
        self.assertIn("403", record["poll_issue"])
        self.assertEqual(accepted["HANCOCK"][2]["counts"], review["counts"])
        report = f.parse(f.store.cached_archive(f.store.read_state()))
        feed = next(r for r in f.read(report, "county_feeds.csv") if r["county"] == "HANCOCK")
        self.assertEqual(feed["issue"], "manual_snapshot")

        # Once the site answers, the automatic response replaces the manual snapshot.
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher()):
            collect_source(f.source, f.settings, f.root, force_download=True)
        _, manifest, accepted = ms.read_bundle(
            f.store.cached_archive(f.store.read_state()), f.source
        )
        self.assertEqual(manifest["counties"]["HANCOCK"]["status"], "current")
        self.assertNotIn("review", manifest["counties"]["HANCOCK"])
        with self.assertRaisesRegex(ValueError, "already has accepted results"):
            import_county(f.source, f.settings, f.root, "HANCOCK", original, template)

    def test_missing_review_values_wrong_original_and_impossible_totals_are_rejected(self):
        f = self.f
        raw, good = synthetic_review(f, "JACKSON")
        county = f.counties["JACKSON"]
        bad = [
            dict(county="HINDS"),
            dict(raw_sha256="0" * 64),
            dict(countywide_report_confirmed=False),
            dict(reviewed_by=""),
            dict(counts={**good["counts"], "Ty Pinkins": -1}),
            dict(reported_candidate_vote_total=100),
            dict(precincts_reporting=2, precincts_total=1),
            dict(observed_at="2026-11-04"),
            dict(report_date="2024-11-05"),
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reviewed_file(raw, county, f.policy, {**good, **changes})
        original, template = f.root / "county.pdf", f.root / "review.json"
        original.write_bytes(raw)
        write_template(f.source, "JACKSON", original, template)
        review = json.loads(template.read_text())
        self.assertTrue(all(v is None for v in review["counts"].values()))
        with self.assertRaises(ValueError):
            reviewed_file(raw, county, f.policy, review)
        with self.assertRaises(FileExistsError):
            write_template(f.source, "JACKSON", original, template)

    def test_one_corrupt_cached_county_does_not_erase_other_county_fallback(self):
        f = self.f
        first, _ = f.collect()
        corrupt = f.rewrite_bundle(
            first, lambda members, _: members.update({f.counties["HINDS"]["member"]: b"invalid"})
        )
        second, _ = f.collect(previous=corrupt, failures=dict.fromkeys(fixtures.EXPECTED, 403))
        _, manifest, accepted = ms.read_bundle(second, f.source)
        self.assertNotIn("HINDS", accepted)
        self.assertIn("PIKE", accepted)
        self.assertEqual(manifest["counties"]["PIKE"]["status"], "stale")

    def test_legacy_bundle_seeds_shadow_owner_without_writing_the_old_state(self):
        f = self.f
        first, _ = f.collect()
        added = {"LAFAYETTE", "LAMAR", "SUNFLOWER"}

        def legacy(members, manifest):
            manifest.update(version=1, policy_sha256=f.policy["compatible_policy_sha256"][0])
            for county in f.policy["counties"]:
                if county["key"] in added or not county.get("url"):
                    members.pop(county["member"])
                if county["key"] in added:
                    manifest["counties"][county["key"]] = dict(
                        status="not_collected",
                        url=None,
                        raw_sha256=None,
                        last_attempt_at=None,
                        last_success_at=None,
                        issue=None,
                        headers={},
                    )

        old = f.rewrite_bundle(first, legacy)
        sha = hashlib.sha256(old.read_bytes()).hexdigest()
        destination = f.store.temporary_download()
        destination.write_bytes(old.read_bytes())
        f.store.commit_download(destination, sha)
        f.store.write_state({"sha256": sha, "source_signature": "legacy"})
        old_state = f.store.state_path.read_bytes()
        shadow = replace(f.source, source_id="ms_2026_general_all")
        store = ArchiveStore(f.root, shadow)
        target = store.temporary_download()
        with patch.object(
            county_files, "fetch_file", side_effect=f.fetcher(failures={"HINDS": 403})
        ):
            county_files.fetch_bundle(shadow, f.settings, target, store=store)
        _, manifest, accepted = ms.read_bundle(target, shadow)
        self.assertEqual(len(accepted), 13)
        self.assertEqual(manifest["counties"]["HINDS"]["status"], "stale")
        self.assertEqual(f.store.state_path.read_bytes(), old_state)

    def test_deadline_defers_work_and_next_cycle_services_deferred_counties_first(self):
        f = self.f
        clock, visits = [0.0], []
        original_fetch = f.fetcher()

        def fetch(*args, **kwargs):
            visits.append(kwargs["url"])
            clock[0] += 100
            return original_fetch(*args, **kwargs)

        with (
            patch.object(county_files.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(county_files, "fetch_file", side_effect=fetch),
        ):
            first = f.store.temporary_download()
            county_files.fetch_bundle(f.source, f.settings, first)
            first_visits = set(visits)
            clock[0] = 0
            second = f.store.temporary_download()
            county_files.fetch_bundle(f.source, f.settings, second, previous=first)
        self.assertGreater(len(set(visits)), len(first_visits))
        self.assertGreaterEqual(len(set(visits)), 2)
        self.assertEqual(len(ms.read_bundle(second, f.source)[2]), len(set(visits)))

    def test_explicit_same_race_replacement_preserves_existing_owners_and_feed_urls(self):
        f = self.f
        image, revision = "sha256:" + "a" * 64, "b" * 40
        new = catalog.entry(
            replace(f.source, source_id="ms_2026_general_all"),
            f.settings,
            image_id=image,
            revision=revision,
            release_path=f.root,
            worker="expansion",
            status="unofficial",
        )
        old_source = replace(
            f.source,
            files=tuple(
                p
                for p in f.source.files
                if p[0]
                not in {f.counties[k]["member"] for k in ("LAFAYETTE", "LAMAR", "SUNFLOWER")}
            ),
            expected_members=tuple(
                [f.source.primary_filename]
                + [
                    p[0]
                    for p in f.source.files
                    if p[0]
                    not in {f.counties[k]["member"] for k in ("LAFAYETTE", "LAMAR", "SUNFLOWER")}
                ]
            ),
            supplemental_members=(),
            collector_overrides=(),
        )
        old = catalog.entry(
            old_source,
            f.settings,
            image_id=image,
            revision=revision,
            release_path=f.root,
            worker="expansion",
            status="unofficial",
        )
        old["races"] = new["races"] = ["MS-SEN"]
        self.assertTrue(catalog.expands_coverage(old, new))
        changed = copy.deepcopy(new)
        changed["source"]["files"] = [
            (name, url + "?other=1") for name, url in new["source"]["files"]
        ]
        self.assertFalse(catalog.expands_coverage(old, changed))
        value = dict(
            version=1,
            generation=0,
            baseline=dict(image_id=image, revision=revision, source_ids=[]),
            sources={
                old_source.source_id: {**old, "enabled": True, "public": True},
                new["source"]["source_id"]: {**new, "replaces": old_source.source_id},
            },
        )
        catalog.validate(value)
        for code in ("NC", "KY"):
            baseline_source = replace(
                old_source,
                source_id=code.lower() + "_2026_general",
                state=code,
                format="zip",
                files=(),
                expected_members=(),
                contests=(),
            )
            baseline = catalog.entry(
                baseline_source,
                f.settings,
                image_id=image,
                revision=revision,
                release_path=f.root,
                worker="baseline",
                status="unofficial",
            )
            baseline["races"] = [code + "-SEN"]
            value["sources"][baseline_source.source_id] = baseline
            value["baseline"]["source_ids"].append(baseline_source.source_id)
        switched = catalog.prospective(value, [new["source"]["source_id"]])
        self.assertFalse(switched["sources"][old_source.source_id]["enabled"])
        self.assertTrue(switched["sources"][new["source"]["source_id"]]["public"])
        for code in ("nc", "ky"):
            name = code + "_2026_general"
            self.assertEqual(
                switched["sources"][name], json.loads(json.dumps(value["sources"][name]))
            )
        with self.assertRaisesRegex(ValueError, "multiple public"):
            value["sources"][new["source"]["source_id"]].update(enabled=True, public=True)
            catalog.validate(value)

    def test_production_county_import_uses_pinned_image_offline_without_database_secrets(self):
        f = self.f
        original, review = f.root / "original.pdf", f.root / "review.json"
        original.write_bytes(b"test")
        review.write_text("{}")
        identity = "sha256:" + "a" * 64
        entry = dict(image_id=identity, source=dict(source_id="ms_2026_general_all"))
        config = dict(EXPANSION_CPUS="0.5", EXPANSION_MEMORY="768m")
        with (
            patch.object(
                containers, "command", return_value='{"outcome":"manual_imported"}'
            ) as run,
            patch.object(containers.subprocess, "run") as cleanup,
        ):
            containers.import_county(
                entry, dict(DATA_DIR=str(f.root)), config, "JACKSON", original, review
            )
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--user") + 1], "10001:10001")
        self.assertNotIn("--env", args)
        self.assertIn(identity, args)
        self.assertIn("/app/data/original.pdf", args)
        self.assertEqual(cleanup.call_args.args[0][-1], args[args.index("--name") + 1])
        with self.assertRaises(ValueError):
            containers.import_county(
                entry, dict(DATA_DIR=str(f.root / "other")), config, "JACKSON", original, review
            )


if __name__ == "__main__":
    unittest.main()
