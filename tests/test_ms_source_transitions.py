"""Configuration changes must not silently discard a county's accepted counts.

Onboarding a reviewed-file county to an automatic URL changes the source contract.
These tests use a patched policy (never the shipped one), so the transition is exercised
without inventing a real county source.
"""

import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_mississippi as fixtures
from test_county_scaling import synthetic_review

from election_pipeline.collectors import county_files
from election_pipeline.county_import import import_county
from election_pipeline.deployment import catalog
from election_pipeline.parsers import ms
from election_pipeline.pipeline import collect_source

NEW_JACKSON = "https://example.com/official/jackson-automatic.pdf"
MOVED_HINDS = "https://example.com/official/hinds-moved.pdf"


class SourceTransitionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MississippiTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def contract(self, change):
        value = copy.deepcopy(self.f.policy)
        counties = {c["key"]: c for c in value["counties"]}
        change(counties)
        return value

    def source_for(self, contract):
        files = tuple(
            sorted((c["member"], c["url"]) for c in contract["counties"] if c.get("url"))
        )
        manual = tuple(c["member"] for c in contract["counties"] if not c.get("url"))
        return replace(self.f.source, files=files, supplemental_members=manual)

    def collect_with(self, contract, previous, failures):
        source = self.source_for(contract)
        destination = self.f.store.temporary_download()
        with (
            patch.object(ms, "policy", return_value=contract),
            patch.object(county_files, "fetch_file", side_effect=self.fetcher(contract, failures)),
        ):
            county_files.fetch_bundle(
                source, self.f.settings, destination, previous=previous, store=self.f.store
            )
            return source, destination, ms.read_bundle(destination, source)

    def fetcher(self, contract, failures):
        # The test fetcher only knows the shipped URLs; transition URLs must fail closed.
        shipped = self.f.fetcher(failures=failures)
        known = {c["url"] for c in self.f.policy["counties"] if c.get("url")}

        def fetch(source, limits, path, conditions, *, url):
            if url not in known:
                self.assertEqual(conditions, {}, "Validators from another URL were reused")
                return county_files.FetchResponse(404, {}, url)
            return shipped(source, limits, path, conditions, url=url)

        return fetch

    def imported_jackson(self):
        f = self.f
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher()):
            collect_source(f.source, f.settings, f.root, force_download=True)
        raw, review = synthetic_review(f, "JACKSON")
        original, review_path = f.root / "jackson.pdf", f.root / "jackson.json"
        original.write_bytes(raw)
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with patch.object(county_files, "fetch_file", side_effect=AssertionError("import HTTP")):
            import_county(f.source, f.settings, f.root, "JACKSON", original, review_path)
        return f.store.cached_archive(f.store.read_state()), review

    def test_manual_snapshot_survives_onboarding_until_the_automatic_source_succeeds(self):
        previous, review = self.imported_jackson()
        contract = self.contract(
            lambda c: c["JACKSON"].update(
                url=NEW_JACKSON, acquisition="automatic", format="pdf_text_electionware"
            )
        )
        _, _, (_, manifest, accepted) = self.collect_with(contract, previous, failures={})
        record = manifest["counties"]["JACKSON"]
        self.assertIn("JACKSON", set(accepted), "Accepted manual counts were discarded")
        self.assertEqual(accepted["JACKSON"][2]["counts"], review["counts"])
        # Provenance stays with the reviewed original, not the new automatic URL.
        self.assertEqual(record["status"], "stale")
        self.assertEqual(record["issue"], "ManualSnapshot")
        self.assertEqual(record["url"], review["source_url"])
        self.assertEqual(record["last_success_at"], review["observed_at"])
        self.assertEqual(record["review"], review)
        self.assertIn("HTTP 404", record["automatic_issue"])
        self.assertEqual(manifest["counties"]["HINDS"]["status"], "current")

    def test_manual_fallback_is_reported_as_a_source_issue_not_a_healthy_feed(self):
        previous, _ = self.imported_jackson()
        contract = self.contract(
            lambda c: c["JACKSON"].update(
                url=NEW_JACKSON, acquisition="automatic", format="pdf_text_electionware"
            )
        )
        source, path, _ = self.collect_with(contract, previous, failures={})
        output = self.f.root / "parsed"
        output.mkdir()
        stats = {}
        with patch.object(ms, "policy", return_value=contract):
            ms.parse(path, source, self.f.settings, output, stats)
        self.assertIn("HTTP 404", stats["county_feed_issues"]["JACKSON"])
        self.assertIn("Some configured county sources are stale or unavailable.", stats["warnings"])
        with (output / "county_feeds.csv").open(encoding="utf-8", newline="") as handle:
            feeds = {r["county"]: r for r in fixtures.csv.DictReader(handle)}
        self.assertEqual(feeds["JACKSON"]["issue"], "manual_snapshot")
        self.assertEqual(feeds["JACKSON"]["collection_status"], "stale")

    def test_changed_automatic_url_does_not_reuse_the_old_url_snapshot(self):
        # Re-pinning is out of scope: the catalog also refuses changed feed URLs.
        f = self.f
        first, _ = f.collect()
        moved = self.contract(lambda c: c["HINDS"].update(url=MOVED_HINDS))
        _, _, (_, manifest, accepted) = self.collect_with(moved, first, failures={})
        self.assertNotIn("HINDS", set(accepted))
        self.assertEqual(manifest["counties"]["HINDS"]["status"], "unavailable")

    def test_manual_record_cannot_masquerade_as_a_current_automatic_check(self):
        previous, _ = self.imported_jackson()
        contract = self.contract(
            lambda c: c["JACKSON"].update(
                url=NEW_JACKSON, acquisition="automatic", format="pdf_text_electionware"
            )
        )
        source, path, _ = self.collect_with(contract, previous, failures={})

        def promote(members, manifest):
            manifest["source_id"] = source.source_id
            record = manifest["counties"]["JACKSON"]
            record.update(
                status="current",
                issue=None,
                url=NEW_JACKSON,
                last_success_at=record["last_attempt_at"],
            )

        forged = self.f.rewrite_bundle(path, promote)
        with (
            patch.object(ms, "policy", return_value=contract),
            self.assertRaisesRegex(ValueError, "manual snapshot"),
        ):
            ms.read_bundle(forged, source)

    def test_successful_automatic_check_replaces_a_manual_snapshot_with_real_bytes(self):
        """Hinds stands in for an onboarded county: reviewed import, then its shipped URL."""
        f = self.f
        hinds = f.counties["HINDS"]
        manual_policy = self.contract(
            lambda c: (
                c["HINDS"].pop("url"),
                c["HINDS"].update(acquisition="reviewed_file", format="reviewed_file"),
            )
        )
        manual_source = self.source_for(manual_policy)
        store = type(f.store)(f.root, manual_source)
        with (
            patch.object(ms, "policy", return_value=manual_policy),
            patch.object(county_files, "fetch_file", side_effect=self.fetcher(manual_policy, {})),
        ):
            collect_source(manual_source, f.settings, f.root, force_download=True)
        original = f.root / "hinds.pdf"
        original.write_bytes(f.raw["HINDS"])
        review = dict(
            state="MS",
            county="HINDS",
            election_date="2026-11-03",
            contest_id="MS-SEN",
            raw_sha256=fixtures.hashlib.sha256(f.raw["HINDS"]).hexdigest(),
            source_url=hinds["url"],
            official_page_url="https://www.co.hinds.ms.us/pgs/results/ElectionNightResults.asp",
            observed_at="2026-11-04T06:00:00+00:00",
            reviewed_by="TEST FIXTURE: values from tests/test_mississippi.py EXPECTED",
            countywide_report_confirmed=True,
            report_generated_at="2026-11-03T23:00:00-06:00",
            report_date="2026-11-03",
            report_time_note=None,
            counts=dict(zip(f.policy["candidates"], fixtures.EXPECTED["HINDS"])),
            write_in_votes=None,
            write_in_label=None,
            reported_candidate_vote_total=None,
            precincts_reporting=108,
            precincts_total=108,
            absentee_scope="unspecified",
            affidavit_scope="unspecified",
        )
        review_path = f.root / "hinds.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with patch.object(ms, "policy", return_value=manual_policy):
            import_county(manual_source, f.settings, f.root, "HINDS", original, review_path)
        previous = store.cached_archive(store.read_state())

        # Onboarded: the shipped policy gives Hinds its automatic URL. First a failure...
        failed = f.store.temporary_download()
        outage = f.fetcher(failures={"HINDS": 503})
        with patch.object(county_files, "fetch_file", side_effect=outage):
            county_files.fetch_bundle(f.source, f.settings, failed, previous=previous)
        _, manifest, accepted = ms.read_bundle(failed, f.source)
        self.assertEqual(manifest["counties"]["HINDS"]["issue"], "ManualSnapshot")
        self.assertEqual(accepted["HINDS"][2]["counts"], review["counts"])
        # ...then a validated automatic response replaces the manual label and review.
        fixed = f.store.temporary_download()
        with patch.object(county_files, "fetch_file", side_effect=f.fetcher()):
            county_files.fetch_bundle(f.source, f.settings, fixed, previous=failed)
        _, manifest, accepted = ms.read_bundle(fixed, f.source)
        record = manifest["counties"]["HINDS"]
        self.assertEqual(
            (record["status"], record["url"], record["issue"]), ("current", hinds["url"], None)
        )
        self.assertNotIn("review", record)
        self.assertEqual(tuple(accepted["HINDS"][2]["counts"].values()), fixtures.EXPECTED["HINDS"])



    def test_catalog_admits_onboarding_but_refuses_a_changed_feed_url(self):
        f = self.f

        def entry(contract):
            value = catalog.entry(
                self.source_for(contract),
                f.settings,
                image_id="sha256:" + "a" * 64,
                revision="b" * 40,
                release_path=f.root,
                worker="expansion",
                status="unofficial",
            )
            value["races"] = ["MS-SEN"]
            return value

        current = entry(f.policy)
        onboarded = entry(
            self.contract(
                lambda c: c["JACKSON"].update(
                    url=NEW_JACKSON, acquisition="automatic", format="pdf_text_electionware"
                )
            )
        )
        moved = entry(self.contract(lambda c: c["HINDS"].update(url=MOVED_HINDS)))
        self.assertTrue(catalog.expands_coverage(current, onboarded))
        self.assertFalse(catalog.expands_coverage(current, moved))


if __name__ == "__main__":
    unittest.main()
