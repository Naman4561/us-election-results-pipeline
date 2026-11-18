"""Synthetic scale/recovery checks; synthetic counts never enter production."""

import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import test_mississippi as fixtures
from pypdf import PdfWriter

from election_pipeline.checks import validate_payload
from election_pipeline.collectors import county_files
from election_pipeline.county_import import import_county, set_house, write_template
from election_pipeline.database import import_fetches
from election_pipeline.deployment import containers
from election_pipeline.parsers import ms
from election_pipeline.parsers.county_reports import reviewed_file
from election_pipeline.pipeline import collect_source


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

    def test_unchanged_county_reports_are_stored_once_and_new_versions_are_kept(self):
        f = self.f
        members = f.store.raw_dir / "members"
        first, _ = f.collect()
        stored = {p.name for p in members.iterdir()}
        _, manifest, accepted = ms.read_bundle(first, f.source)
        self.assertEqual(manifest["version"], 3)
        self.assertEqual(stored, {r[1]["raw_sha256"] + ".bin" for r in accepted.values()})
        with zipfile.ZipFile(first) as archive:
            self.assertTrue(all(archive.read(r[0]["member"]) == b"" for r in accepted.values()))

        # Unchanged polls add a small manifest-only bundle and no county bytes.
        second, _ = f.collect(previous=first)
        self.assertNotEqual(first, second)
        self.assertEqual({p.name for p in members.iterdir()}, stored)
        self.assertLess(second.stat().st_size, 100_000)
        self.assertEqual(ms.read_bundle(second, f.source)[2].keys(), accepted.keys())

        # A changed report is a new version: both the old and new bytes are retained.
        changed, real = f.raw["PIKE"] + b" ", ms.read_county

        def read(raw, county, contract):
            return real(f.raw["PIKE"] if raw == changed else raw, county, contract)

        with patch.object(ms, "read_county", side_effect=read):
            third, _ = f.collect(previous=second, bodies={"PIKE": changed})
        current = {p.name for p in members.iterdir()}
        self.assertEqual(current - stored, {hashlib.sha256(changed).hexdigest() + ".bin"})
        self.assertLessEqual(stored, current)
        self.assertEqual(ms.read_bundle(first, f.source)[2]["PIKE"][3], f.raw["PIKE"])

        # A missing or altered stored report is refused, never read as empty.
        pike = members / (hashlib.sha256(changed).hexdigest() + ".bin")
        pike.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            ms.read_bundle(third, f.source)
        pike.unlink()
        with self.assertRaisesRegex(ValueError, "member store"):
            ms.read_bundle(third, f.source)

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
        self.assertEqual((report["counties_with_counts"], report["output_rows"]), (82, 246))
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

        # A later check that finds the same file refreshes the observation time only.
        later = {**review, "observed_at": "2026-11-12T03:42:54+00:00"}
        (f.root / "later.json").write_text(json.dumps(later), encoding="utf-8")
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("import HTTP")):
            result = import_county(
                f.source, f.settings, f.root, "HANCOCK", original, f.root / "later.json"
            )
            repeat = import_county(f.source, f.settings, f.root, "HANCOCK", original, template)
        self.assertEqual(result["outcome"], "manual_confirmed", result)
        self.assertEqual(repeat["outcome"], "already_imported")
        _, manifest, accepted = ms.read_bundle(
            f.store.cached_archive(f.store.read_state()), f.source
        )
        record = manifest["counties"]["HANCOCK"]
        self.assertEqual(record["last_success_at"], later["observed_at"])
        self.assertEqual(record["issue"], "ManualSnapshot")
        self.assertEqual(accepted["HANCOCK"][2]["counts"], review["counts"])
        self.assertEqual(
            ms.stamp(accepted["HANCOCK"][2]["report_generated_at"]),
            ms.stamp(review["report_generated_at"]),
        )

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

    def test_certified_recap_replaces_an_automatic_feed_and_ends_its_polling(self):
        f = self.f
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher()):
            collect_source(f.source, f.settings, f.root, force_download=True)
        _, before, accepted = ms.read_bundle(f.store.cached_archive(f.store.read_state()), f.source)
        self.assertEqual(before["counties"]["PIKE"]["status"], "current")

        raw, review = synthetic_review(f, "PIKE")
        sos = "https://www.sos.ms.gov/elections/electionresults/2026General/Pike%20County.pdf"
        review.update(
            source_url=sos,
            official_page_url="https://www.sos.ms.gov/elections-voting/election-results",
            observed_at="2026-11-17T21:00:00+00:00",
            certified=True,
            certified_on="2026-11-11",
        )
        original, path = f.root / "pike.pdf", f.root / "pike.json"
        original.write_bytes(raw)

        # Without the certified flag it is an ordinary manual copy, which a fed county refuses.
        path.write_text(json.dumps({**review, "certified": False}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configured URL"):
            import_county(f.source, f.settings, f.root, "PIKE", original, path)
        for changes in (
            dict(certified_on=None),
            dict(certified_on="2026-11-02"),
            dict(certified_on="2026-11-18"),
            dict(certified="yes"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reviewed_file(raw, f.counties["PIKE"], f.policy, {**review, **changes})

        path.write_text(json.dumps(review), encoding="utf-8")
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("import HTTP")):
            result = import_county(f.source, f.settings, f.root, "PIKE", original, path)
        self.assertEqual(result["outcome"], "manual_imported", result)
        _, manifest, accepted = ms.read_bundle(
            f.store.cached_archive(f.store.read_state()), f.source
        )
        record = manifest["counties"]["PIKE"]
        self.assertEqual(
            (record["status"], record["issue"], record["url"]), ("stale", ms.COUNTY_CERTIFIED, sos)
        )
        self.assertEqual(accepted["PIKE"][2]["counts"], review["counts"])

        # Later polls leave the certified county alone and still collect the others.
        requested = []
        fetch = f.fetcher()

        def tracked(source, limits, target, conditions, *, url):
            requested.append(url)
            return fetch(source, limits, target, conditions, url=url)

        with patch.object(county_files, "fetch_file", side_effect=tracked):
            collect_source(f.source, f.settings, f.root, force_download=True)
        self.assertNotIn(f.counties["PIKE"]["url"], requested)
        self.assertIn(f.counties["HINDS"]["url"], requested)
        archive = f.store.cached_archive(f.store.read_state())
        _, manifest, accepted = ms.read_bundle(archive, f.source)
        self.assertEqual(manifest["counties"]["PIKE"], record)
        self.assertEqual(manifest["counties"]["HINDS"]["status"], "current")

        report = f.parse(archive)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertNotIn("PIKE", report["county_feed_issues"])
        feed = next(r for r in f.read(report, "county_feeds.csv") if r["county"] == "PIKE")
        self.assertEqual(feed["issue"], "certified")
        payload, _ = f.payload(report)
        self.assertEqual(payload["county_feeds"]["PIKE"]["acquisition"], "certified")
        self.assertEqual(payload["county_collection"]["certified"], 1)

        # Only another certified recap may replace it.
        plain = {
            **review,
            "certified": False,
            "certified_on": None,
            "source_url": f.counties["PIKE"]["url"],
            "observed_at": "2026-11-18T00:00:00+00:00",
        }
        path.write_text(json.dumps(plain), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Only another certified recap"):
            import_county(f.source, f.settings, f.root, "PIKE", original, path)

    def certify_all(self, skip=()):
        """Every county but ``skip`` takes a synthetic certified recap with its House TOTAL."""
        f = self.f
        original, _ = f.collect()
        districts = ms.house_policy()
        imports = {}
        for county in f.policy["counties"]:
            key = county["key"]
            if key in skip:
                continue
            raw, review = synthetic_review(f, key)
            contest = districts["primary_district"][key]
            review.update(
                source_url="https://www.sos.ms.gov/elections/electionresults/2026General/x.pdf",
                certified=True,
                certified_on="2026-11-04",
                house={contest: dict.fromkeys(districts["districts"][contest], 5)},
            )
            record = dict(
                status="stale",
                url=review["source_url"],
                raw_sha256=review["raw_sha256"],
                issue=ms.COUNTY_CERTIFIED,
                headers={},
                last_success_at=review["observed_at"],
                last_attempt_at=review["observed_at"],
                review=review,
            )
            parsed = reviewed_file(raw, county, f.policy, review)
            imports[key] = (county, record, parsed, raw)
        target = f.store.temporary_download()
        fetched = county_files.fetch_bundle(
            f.source,
            f.settings,
            target,
            previous=original,
            store=f.store,
            imports=imports,
            offline=True,
        )
        archive, _ = f.store.commit_download(target, fetched.sha256)
        f.store.write_state({**f.store.read_state(), "sha256": fetched.sha256})
        return archive

    def test_house_districts_turn_on_only_once_every_county_has_a_certified_count(self):
        f = self.f
        archive = self.certify_all(skip={"WALTHALL"})
        report = f.parse(archive)
        self.assertEqual(report["contests"], ["MS-SEN"])
        payload, _ = f.payload(report)
        self.assertEqual(set(payload["races"]), {"MS-SEN"})

        with self.assertRaisesRegex(ValueError, "WALTHALL"):
            set_house(f.source, f.settings, f.root, True)
        result = set_house(f.source, f.settings, f.root, True, ["WALTHALL"])
        self.assertEqual(result["outcome"], "house_enabled", result)
        self.assertEqual(
            set_house(f.source, f.settings, f.root, True, ["WALTHALL"])["outcome"], "unchanged"
        )
        report = f.parse(f.store.cached_archive(f.store.read_state()))
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["contests"], ["MS-01", "MS-02", "MS-03", "MS-04", "MS-SEN"])
        self.assertEqual(report["house_coverage"]["MS-03"], ["WALTHALL"])
        self.assertEqual(report["house_coverage"]["MS-01"], [])
        payload, _ = f.payload(report)
        self.assertEqual(payload["races"]["MS-03"]["missing_counties"], ["Walthall"])
        self.assertEqual(payload["races"]["MS-02"]["missing_counties"], [])
        self.assertNotIn("missing_counties", payload["races"]["MS-SEN"])

        # Polls carry the setting forward; every county is certified, so none is fetched.
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("polled")):
            collect_source(f.source, f.settings, f.root, force_download=True)
        manifest = ms.read_bundle(f.store.cached_archive(f.store.read_state()), f.source)[1]
        self.assertEqual(manifest["options"], {"house": True})

        set_house(f.source, f.settings, f.root, False)
        report = f.parse(f.store.cached_archive(f.store.read_state()))
        self.assertEqual(report["contests"], ["MS-SEN"])

    def test_house_counts_need_a_certified_recap_and_the_district_candidates(self):
        f = self.f
        raw, review = synthetic_review(f, "NESHOBA")
        house = {"MS-03": dict.fromkeys(ms.house_policy()["districts"]["MS-03"], 7)}
        certified = {**review, "certified": True, "certified_on": "2026-11-04", "house": house}
        self.assertEqual(
            reviewed_file(raw, f.counties["NESHOBA"], f.policy, certified)["house"], house
        )
        for changes in (
            dict(certified=False, certified_on=None),
            dict(house={"MS-05": house["MS-03"]}),
            dict(house={"MS-03": {**house["MS-03"], "Someone Else": 1}}),
            dict(house={"MS-03": {**house["MS-03"], "Michael Guest": -1}}),
            dict(house={}),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "House"):
                reviewed_file(raw, f.counties["NESHOBA"], f.policy, {**certified, **changes})

    def test_certified_recap_allows_a_long_scan_that_an_ordinary_review_refuses(self):
        f = self.f
        writer = PdfWriter()
        for _ in range(51):
            writer.add_blank_page(width=100, height=100)
        target = io.BytesIO()
        writer.write(target)
        raw = target.getvalue()
        _, review = synthetic_review(f, "HINDS")
        review.update(raw_sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(ValueError, "supported contract"):
            reviewed_file(raw, f.counties["HINDS"], f.policy, review)
        certified = {**review, "certified": True, "certified_on": "2026-11-04"}
        parsed = reviewed_file(raw, f.counties["HINDS"], f.policy, certified)
        self.assertEqual(parsed["counts"], review["counts"])

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

    def test_production_county_import_uses_the_release_image_offline_without_secrets(self):
        f = self.f
        original, review = f.root / "original.pdf", f.root / "review.json"
        original.write_bytes(b"test")
        review.write_text("{}")
        identity = "sha256:" + "a" * 64
        source = "ms_2026_general_all"
        config = dict(JOB_CPUS="0.5", JOB_MEMORY="768m")
        with (
            patch.object(
                containers, "command", return_value='{"outcome":"manual_imported"}'
            ) as run,
            patch.object(containers.subprocess, "run") as cleanup,
        ):
            containers.import_county(
                identity, source, dict(DATA_DIR=str(f.root)), config, "JACKSON", original, review
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
                identity,
                source,
                dict(DATA_DIR=str(f.root / "other")),
                config,
                "JACKSON",
                original,
                review,
            )


if __name__ == "__main__":
    unittest.main()
