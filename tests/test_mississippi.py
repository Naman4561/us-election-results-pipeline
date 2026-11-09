"""Original county captures, failed-source recovery, explicit coverage and safe publication."""

import csv
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from loading import copies, migrated

from election_pipeline import accuracy, publication
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors import county_files
from election_pipeline.dashboard.server import (
    STATIC,
    demo_payload,
    make_payload,
    with_county_feeds,
)
from election_pipeline.database import (
    connect,
    load_parsed,
)
from election_pipeline.parsers import ms
from election_pipeline.parsing import parse_source
from election_pipeline.pipeline import collect_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore, member_path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/mississippi/reports.zip"
EXPECTED = {
    "DESOTO": (28103, 24780, 1301),
    "HANCOCK": (4538, 11441, 753),
    "HINDS": (61793, 14702, 970),
    "LAUDERDALE": (10528, 10892, 454),
    "MADISON": (22712, 22364, 690),
    "MONROE": (5441, 7150, 174),
    "NESHOBA": (2825, 5099, 137),
    "PIKE": (7333, 5150, 197),
    "RANKIN": (18513, 31654, 1164),
    "WARREN": (8117, 6148, 378),
    "LAFAYETTE": (8383, 8265, 368),
    "LAMAR": (7481, 12914, 625),
    "SUNFLOWER": (5296, 1655, 79),
}


class MississippiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ms_2026_general_all"]
        self.policy = ms.policy()
        self.counties = {c["key"]: c for c in self.policy["counties"]}
        with zipfile.ZipFile(FIXTURE) as archive:
            self.raw = {
                c["key"]: archive.read(c["member"]) if c["member"] in archive.namelist() else b""
                for c in self.policy["counties"]
                if c.get("url")
            }
        with zipfile.ZipFile(ROOT / "tests/fixtures/mississippi/expansion.zip") as archive:
            self.raw.update(
                {
                    c["key"]: archive.read(c["member"])
                    for c in self.policy["counties"]
                    if c["member"] in archive.namelist()
                }
            )
        self.store = ArchiveStore(self.root, self.source)

    def fetcher(self, failures=None, bodies=None, conditional=False):
        by_url = {c["url"]: c for c in self.policy["counties"] if c.get("url")}

        def fetch(source, limits, path, conditions, *, url):
            county = by_url[url]
            key = county["key"]
            self.assertLessEqual(limits.timeout_seconds, 12)
            self.assertLessEqual(limits.max_download_bytes, 8_000_000)
            if failures and key in failures:
                return FetchResponse(failures[key], {}, url)
            if conditional and conditions:
                return FetchResponse(304, {"etag": '"' + key + '"'}, url)
            raw = (bodies or {}).get(key, self.raw[key])
            path.write_bytes(raw)
            return FetchResponse(
                200, {"etag": '"' + key + '"'}, url, len(raw), hashlib.sha256(raw).hexdigest()
            )

        return fetch

    def collect(self, previous=None, **options):
        destination = self.store.temporary_download()
        with patch.object(county_files, "fetch_file", side_effect=self.fetcher(**options)):
            result = county_files.fetch_bundle(
                self.source,
                self.settings,
                destination,
                previous=previous,
                store=self.store,
            )
        path, _ = self.store.commit_download(destination, result.sha256)
        return path, result

    def parse(self, path):
        return parse_source(
            self.source,
            self.settings,
            self.root,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def read(self, report, filename):
        with (Path(report["output_dir"]) / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def rewrite_bundle(self, path, change):
        with zipfile.ZipFile(path) as archive:
            members = {n: archive.read(n) for n in archive.namelist()}
        manifest = json.loads(members[self.source.primary_filename])
        if manifest["version"] == 3:
            # Inline the stored county reports so the mutated copy stands alone (version 2).
            for county in self.policy["counties"]:
                sha = manifest["counties"][county["key"]].get("raw_sha256")
                if sha and not members[county["member"]]:
                    members[county["member"]] = member_path(self.store.raw_dir, sha).read_bytes()
            manifest["version"] = 2
        change(members, manifest)
        members[self.source.primary_filename] = json.dumps(manifest).encode()
        target = self.root / "mutated.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        return target

    def payload(self, report):
        rows = self.read(report, "county_results.csv")
        contests = self.read(report, "contest_results.csv")
        for row in rows + contests:
            row["total_votes"] = int(row["total_votes"])
        feeds = [
            {k: v or None for k, v in row.items()} for row in self.read(report, "county_feeds.csv")
        ]
        for row in feeds:
            for name in ("precincts_reporting", "precincts_total", "write_in_votes"):
                row[name] = int(row[name]) if row[name] is not None else None
        geo = json.loads((STATIC / "ms-map.json").read_text())
        return make_payload(
            geo,
            {"publication_id": "fixture", "status": "unofficial"},
            rows,
            contests,
            state="MS",
            county_feeds=feeds,
        ), feeds

    def test_all_original_reports_have_exact_known_named_candidate_counts(self):
        with zipfile.ZipFile(FIXTURE) as archive:
            provenance = json.loads(archive.read("provenance.json"))
            for item in provenance["source_captures"]:
                self.assertEqual(
                    hashlib.sha256(archive.read(item["member"])).hexdigest(), item["sha256"]
                )
        for key, expected in EXPECTED.items():
            with self.subTest(county=key):
                result = ms.read_county(self.raw[key], self.counties[key], self.policy)
                self.assertEqual(tuple(result["counts"].values()), expected)
        self.assertIsNone(
            ms.read_county(self.raw["RANKIN"], self.counties["RANKIN"])["write_in_votes"]
        )
        self.assertEqual(
            ms.read_county(self.raw["WARREN"], self.counties["WARREN"])["write_in_label"],
            "Unadjudicated Write-In",
        )

    def test_windows_policy_line_endings_are_canonical_but_changed_counts_are_rejected(self):
        raw = (ROOT / "src/election_pipeline/resources/ms_2026_counties.json").read_bytes()
        resource = MagicMock()
        resource.joinpath.return_value.read_bytes.return_value = raw.replace(
            b"\r\n", b"\n"
        ).replace(b"\n", b"\r\n")
        with patch.object(ms, "files", return_value=resource):
            self.assertEqual(ms.policy(), self.policy)
            resource.joinpath.return_value.read_bytes.return_value = raw.replace(b"28103", b"28104")
            with self.assertRaisesRegex(ValueError, "fingerprint changed"):
                ms.policy()

    def test_collection_parse_load_contract_and_replay_preserve_unknown_coverage(self):
        path, _ = self.collect()
        report = self.parse(path)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(
            (report["output_rows"], report["county_feed_rows"], report["counties_with_counts"]),
            (39, 82, 13),
        )
        self.assertFalse(report["coverage_complete"])
        # Mississippi publishes no statewide totals; they are summed from accepted counties.
        self.assertEqual(report["county_reconciliation"], "not_available")
        contests = {
            r["choice"]: int(r["total_votes"]) for r in self.read(report, "contest_results.csv")
        }
        self.assertEqual(
            contests, {"Scott Colom": 191063, "Cindy Hyde-Smith": 162214, "Ty Pinkins": 7290}
        )
        feeds = {r["county"]: r for r in self.read(report, "county_feeds.csv")}
        self.assertEqual(feeds["JACKSON"]["collection_status"], "not_collected")
        self.assertEqual(feeds["JACKSON"]["raw_sha256"], "")
        self.assertEqual(feeds["JACKSON"]["write_in_votes"], "")
        self.assertEqual(feeds["MONROE"]["absentee_scope"], "excluded")
        self.assertEqual(feeds["NESHOBA"]["precincts_total"], "")
        loaded = copies(self.source, self.settings, self.store, report["output_dir"], 99)
        values = loaded[("ms", "county_feeds")]
        self.assertEqual(len(values), 82)
        self.assertEqual(values[0][:2], (99, 2))
        self.assertEqual(len(loaded[("landing", "results")]), 39)
        with patch.object(
            county_files, "fetch_file", side_effect=AssertionError("Replay made HTTP")
        ):
            self.assertEqual(self.parse(path)["outcome"], "already_parsed")

    def test_failed_county_does_not_stop_others_or_become_zero(self):
        path, _ = self.collect(failures={"HINDS": 403})
        report = self.parse(path)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["counties_with_counts"], 12)
        feeds = {r["county"]: r for r in self.read(report, "county_feeds.csv")}
        self.assertEqual(feeds["HINDS"]["collection_status"], "unavailable")
        self.assertNotIn("HINDS", {r["county"] for r in self.read(report, "county_results.csv")})

    def test_failure_retains_validated_bytes_and_original_success_timestamp(self):
        first, _ = self.collect()
        _, manifest, _ = ms.read_bundle(first, self.source)
        second, _ = self.collect(previous=first, failures={"HINDS": 403}, conditional=True)
        _, updated, _ = ms.read_bundle(second, self.source)
        old, new = manifest["counties"]["HINDS"], updated["counties"]["HINDS"]
        self.assertEqual(new["status"], "stale")
        self.assertEqual(new["raw_sha256"], old["raw_sha256"])
        self.assertEqual(new["last_success_at"], old["last_success_at"])
        self.assertEqual(updated["counties"]["PIKE"]["status"], "current")
        report = self.parse(second)
        self.assertEqual(report["counties_with_counts"], 13)
        self.assertIn("HINDS", report["county_feed_issues"])

    def test_changed_scan_is_quarantined_and_needs_review_while_other_counties_continue(self):
        first, _ = self.collect()
        modified = self.raw["MADISON"] + b"\n"
        second, _ = self.collect(previous=first, bodies={"MADISON": modified})
        _, manifest, _ = ms.read_bundle(second, self.source)
        record = manifest["counties"]["MADISON"]
        self.assertEqual(record["status"], "stale")
        self.assertTrue(record["issue"].startswith("ReviewRequired:"))
        self.assertEqual((self.root / record["quarantine_path"]).read_bytes(), modified)
        self.assertEqual(manifest["counties"]["RANKIN"]["status"], "current")
        feed = {r["county"]: r for r in self.read(self.parse(second), "county_feeds.csv")}
        self.assertEqual(feed["MADISON"]["issue"], "review_required")

    def test_http_200_challenge_cannot_replace_the_accepted_county(self):
        first, _ = self.collect()
        second, _ = self.collect(
            previous=first,
            bodies={"RANKIN": b"<title>Captcha</title>Enable JavaScript and cookies"},
        )
        _, manifest, _ = ms.read_bundle(second, self.source)
        self.assertEqual(manifest["counties"]["RANKIN"]["status"], "stale")

    def test_all_unavailable_without_a_prior_snapshot_is_an_explicit_failure(self):
        with self.assertRaisesRegex(FetchFailure, "No validated county reports"):
            self.collect(failures=dict.fromkeys(EXPECTED, 403))

    def test_downward_official_correction_is_accepted_without_a_high_water_mark(self):
        first, _ = self.collect()
        corrected = self.raw["RANKIN"]
        for before, after in [
            (b"31,654", b"31,650"),
            (b"27,486", b"27,482"),
            (b"51,331", b"51,327"),
            (b"44,478", b"44,474"),
        ]:
            corrected = corrected.replace(before, after)
        second, _ = self.collect(previous=first, bodies={"RANKIN": corrected})
        _, manifest, accepted = ms.read_bundle(second, self.source)
        self.assertEqual(manifest["counties"]["RANKIN"]["status"], "current")
        self.assertEqual(accepted["RANKIN"][2]["counts"]["Cindy Hyde-Smith"], 31650)

    def test_wrong_election_county_duplicate_extra_choice_and_total_mismatch_are_rejected(self):
        text = ms.pdf_text(self.raw["DESOTO"])
        cases = [
            text.replace("November 3, 2026", "November 3, 2024"),
            text.replace("Desoto County", "Unrelated County"),
            text.replace(
                "D Scott Colom 28,103 51.80%", "D Scott Colom 28,103 51.80%\nD Scott Colom 0 0.00%"
            ),
            text.replace("Write-In Totals 68", "D Extra Candidate 0 0.00%\nWrite-In Totals 68"),
            text.replace("R Cindy Hyde-Smith", "D Cindy Hyde-Smith"),
            text.replace("Ty Pinkins 1,301", "Ty Pinkins -1"),
        ]
        for index, mutated in enumerate(cases):
            with (
                self.subTest(case=index),
                patch.object(ms, "pdf_text", return_value=mutated),
                self.assertRaises(ValueError),
            ):
                ms.read_county(self.raw["DESOTO"], self.counties["DESOTO"])
        text = ms.pdf_text(self.raw["PIKE"]).replace(
            "Total Votes Cast 12,684", "Total Votes Cast 12,685"
        )
        with (
            patch.object(ms, "pdf_text", return_value=text),
            self.assertRaisesRegex(ValueError, "reconcile"),
        ):
            ms.read_county(self.raw["PIKE"], self.counties["PIKE"])

    def test_manifest_missing_county_corrupt_hash_and_fake_unavailable_are_rejected(self):
        path, _ = self.collect()
        changes = [
            lambda m, d: d["counties"].pop("JACKSON"),
            lambda m, d: d["counties"]["HINDS"].update(raw_sha256="0" * 64),
            lambda m, d: d["counties"]["HINDS"].update(
                status="unavailable", raw_sha256=None, last_success_at=None
            ),
            lambda m, d: d.update(election_date="2024-11-05"),
        ]
        for change in changes:
            with self.assertRaises(ValueError):
                ms.read_bundle(self.rewrite_bundle(path, change), self.source)

    def test_outer_collection_dispatch_uses_county_backend_and_force_bypasses_conditions(self):
        seen = []

        def fetch(*args, **kwargs):
            seen.append(args[3])
            return self.fetcher()(*args, **kwargs)

        with patch.object(county_files, "fetch_file", side_effect=fetch):
            one = collect_source(self.source, self.settings, self.root, force_download=True)
            two = collect_source(self.source, self.settings, self.root, force_download=True)
        self.assertEqual(one["outcome"], "downloaded", one)
        self.assertEqual(two["outcome"], "downloaded", two)
        self.assertTrue(all(not conditions for conditions in seen))
        self.assertEqual(self.parse(self.store.archive_path(two["sha256"]))["outcome"], "parsed")

    def test_dashboard_coverage_age_and_unknowns_do_not_turn_into_statewide_totals(self):
        path, _ = self.collect()
        payload, feeds = self.payload(self.parse(path))
        self.assertEqual(payload["county_collection"]["with_results"], 13)
        self.assertFalse(payload["county_collection"]["complete"])
        self.assertEqual(payload["county_feeds"]["JACKSON"]["collection_status"], "not_collected")
        self.assertNotIn("JACKSON", payload["races"]["MS-SEN"]["counties"])
        now = max(
            ms.stamp(r["last_success_at"]) for r in feeds if r["last_success_at"]
        ) + timedelta(minutes=16)
        aged = with_county_feeds(
            payload, json.loads((STATIC / "ms-map.json").read_text()), feeds, now=now
        )
        self.assertEqual(aged["county_collection"]["stale"], 13)
        self.assertEqual(aged["races"], payload["races"])
        with self.assertRaises(ValueError):
            with_county_feeds(payload, json.loads((STATIC / "ms-map.json").read_text()), feeds[:-1])

    def test_publication_targets_and_map(self):
        targets = publication.targets({"state": "MS", "dataset": "election_results"})
        self.assertEqual(
            {t[2] for t in targets},
            {
                ("serving", "county_results"),
                ("serving", "contest_results"),
                ("ms", "county_feed_status"),
            },
        )
        geo = json.loads((STATIC / "ms-map.json").read_text())
        self.assertEqual((len(geo["counties"]), len(geo["districts"])), (82, 4))
        self.assertEqual(
            {c["fips"] for c in geo["counties"]}, {c["fips"] for c in self.policy["counties"]}
        )
        self.assertEqual(demo_payload(geo, "MS")["county_collection"]["not_collected"], 69)

    def test_partial_reference_cannot_claim_statewide_independent_agreement(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.side_effect = [
            {"state": "MS", "dataset": "election_results"},
            {"n": 10},
        ]
        reference = self.root / "reference.csv"
        reference.write_text("election_date,contest_id,choice,total_votes\n")
        with (
            patch.object(accuracy, "connect", return_value=conn),
            self.assertRaisesRegex(ValueError, "Partial county"),
        ):
            accuracy.compare_publication(
                {},
                "11111111-1111-1111-1111-111111111111",
                reference,
                evidence_dir=self.root / "accuracy",
                reference_url=self.source.url,
                observed_at="2026-11-05T12:00:00+00:00",
                same_reporting_cutoff=True,
            )

    def test_reader_pins_votes_and_county_provenance_then_reuses_the_same_publication(self):
        from election_pipeline.dashboard.server import PublishedResults

        path, _ = self.collect()
        report = self.parse(path)
        _, feeds = self.payload(report)
        counties, contests = (
            self.read(report, "county_results.csv"),
            self.read(report, "contest_results.csv"),
        )
        for row in counties + contests:
            row["total_votes"] = int(row["total_votes"])
        metadata = {"publication_id": "ms-frozen", "release_id": 1, "status": "unofficial"}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        role = {"rolsuper": False, "rolcreatedb": False, "rolcreaterole": False}
        responses = [MagicMock() for _ in range(9)]
        responses[1].fetchone.return_value = responses[7].fetchone.return_value = role
        responses[2].fetchone.return_value = responses[8].fetchone.return_value = metadata
        responses[3].fetchall.return_value = counties
        responses[4].fetchall.return_value = contests
        responses[5].fetchall.return_value = feeds
        conn.execute.side_effect = responses
        geo = json.loads((STATIC / "ms-map.json").read_text())
        provider = PublishedResults({}, geo, self.source.source_id, state="MS")
        provider.TTL_SECONDS = 0  # exercise the per-publication cache, not the short reuse
        with patch("election_pipeline.dashboard.server.psycopg.connect", return_value=conn):
            first, second = provider(), provider()
        self.assertEqual(first["races"], second["races"])
        self.assertEqual(first["county_collection"]["with_results"], 13)
        for call in conn.execute.call_args_list[3:6]:
            self.assertEqual(call.args[1], ("ms-frozen",))
        self.assertEqual(len(conn.execute.call_args_list), 9)


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class MississippiDatabaseTests(unittest.TestCase):
    def test_load_publish_reader_provenance_and_failed_validation_keep_live_publication(self):
        db = migrated()
        with connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        fixture = MississippiTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path, _ = fixture.collect()
        report = fixture.parse(path)
        loaded = load_parsed(
            fixture.source, fixture.settings, fixture.root, Path(report["output_dir"]), db
        )
        self.assertEqual(loaded["outcome"], "succeeded", loaded)
        prepared = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        published = publication.publish_prepared(ROOT, db, prepared["candidate_id"])
        self.assertEqual(published["outcome"], "published", published)
        with connect(db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) AS n FROM ms.county_feed_status").fetchone()["n"], 82
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) AS n FROM serving.county_results WHERE state='MS'"
                ).fetchone()["n"],
                39,
            )
            conn.execute("SET ROLE election_dashboard")
            conn.execute("SELECT * FROM ms.county_feed_status").fetchall()
            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(current_user,'ms.county_feeds','SELECT') AS ok"
                ).fetchone()["ok"]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_table_privilege(current_user,"
                    "'ms.county_feed_status','UPDATE') AS ok"
                ).fetchone()["ok"]
            )
            conn.execute("RESET ROLE")
            # A county losing a candidate's count no longer matches its accepted report.
            conn.execute(
                "DELETE FROM landing.results WHERE release_id=%s AND county='HINDS' "
                "AND choice='Scott Colom'",
                (loaded["release_id"],),
            )
        with self.assertRaisesRegex(ValueError, "dbt build failed"):
            publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        with connect(db) as conn:
            current = conn.execute(
                "SELECT publication_id FROM serving.current_publications WHERE source_id=%s",
                (fixture.source.source_id,),
            ).fetchone()
            self.assertEqual(str(current["publication_id"]), published["publication_id"])

        # Capacity check only: the 69 additions are explicitly synthetic test reviews.
        from test_county_scaling import all_imports

        target = fixture.store.temporary_download()
        fetched = county_files.fetch_bundle(
            fixture.source,
            fixture.settings,
            target,
            previous=path,
            imports=all_imports(fixture),
            offline=True,
        )
        complete, _ = fixture.store.commit_download(target, fetched.sha256)
        scaled = fixture.parse(complete)
        loaded = load_parsed(
            fixture.source, fixture.settings, fixture.root, Path(scaled["output_dir"]), db
        )
        self.assertEqual(loaded["outcome"], "succeeded", loaded)
        prepared = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        published = publication.publish_prepared(ROOT, db, prepared["candidate_id"])
        self.assertEqual(published["outcome"], "published", published)
        with connect(db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) AS n FROM serving.county_results WHERE publication_id=%s",
                    (published["publication_id"],),
                ).fetchone()["n"],
                246,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) AS n FROM ms.county_feed_status "
                    "WHERE publication_id=%s AND issue='manual_snapshot'",
                    (published["publication_id"],),
                ).fetchone()["n"],
                69,
            )


if __name__ == "__main__":
    unittest.main()
