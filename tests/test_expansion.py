"""Real zero/blank captures plus synthetic changes; no election-night accuracy claim."""

import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from election_pipeline import bundles, publication
from election_pipeline.collect import FetchResponse
from election_pipeline.dashboard.server import STATIC, make_payload, party_group
from election_pipeline.database import (
    connect,
    connection_settings,
    converted_rows,
    load_parsed,
    verify,
)
from election_pipeline.parsers import mn
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/expansion"


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")

    def members(self, code):
        if code == "MN":
            return {
                **{
                    name: (FIXTURES / ("mn-" + name)).read_bytes()
                    for name in (
                        "senate-county.txt",
                        "senate.txt",
                        "house-county.txt",
                        "house.txt",
                        "counties.txt",
                    )
                },
                "election.html": b"Unofficial Results Tuesday, November 3, 2026",
            }
        if code == "NE":
            return {
                "results.csv": (FIXTURES / "ne.csv").read_bytes(),
                "election.html": b"Unofficial Results General Election November 3, 2026",
            }
        return {"results.html": (FIXTURES / "tx.html").read_bytes()}

    def parse(self, code, mutate=None):
        self.source = self.sources[code.lower() + "_2026_general"]
        if code == "TX":
            # Archived legacy fixtures retain their original source contract.
            self.source = replace(
                self.source,
                url="https://electionresults.sos.state.tx.us/results.html",
                primary_filename="results.html",
                expected_members=("results.html",),
            )
        self.store = ArchiveStore(self.root, self.source)
        members = self.members(code)
        if mutate:
            mutate(members)
        path = self.root / "input.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        target = self.store.archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def test_real_captures_parse_verify_and_convert_without_inventing_counts(self):
        for code, records in [("MN", 506), ("NE", 9), ("TX", 28)]:
            with self.subTest(code=code):
                report = self.parse(code)
                self.assertEqual(report["outcome"], "parsed", report)
                self.assertEqual(report["output_rows"], records)
                directory = Path(report["output_dir"])
                verify(self.source, self.settings, self.store, directory)
                contract = state_adapter(code).contract("election_results")
                converted = list(
                    converted_rows(
                        directory / contract.filename, contract.fields, self.source, 77, records
                    )
                )
                self.assertEqual(converted[0][:2], (77, 2))
                self.assertEqual(converted[0][-3], None if code == "TX" else 0)
                self.assertEqual(report["counts_available"], code != "TX")
                self.assertEqual(report["all_counts_zero"], code != "TX")
                if code == "MN":
                    self.assertEqual(report["county_counts"], {"MN-SEN": 87, "MN-01": 21})

    def test_mn_nonzero_official_and_county_agreement_and_correction(self):
        def positive(members):
            for name in ("senate-county.txt", "senate.txt"):
                rows = list(csv.reader(io.StringIO(members[name].decode()), delimiter=";"))
                for row in rows:
                    # Give every candidate one vote in every county.
                    row[13], row[15] = ("1", "5") if "county" in name else ("87", "435")
                members[name] = ("\n".join(";".join(row) for row in rows)).encode()

        good = self.parse("MN", positive)
        self.assertEqual(good["outcome"], "parsed", good)
        self.assertFalse(good["all_counts_zero"])

        def mismatched(members):
            positive(members)
            members["senate.txt"] = members["senate.txt"].replace(b";87;", b";86;", 1)

        self.assertEqual(self.parse("MN", mismatched)["outcome"], "validation_failed")

    def test_missing_mn_county_detected_even_when_all_votes_zero(self):
        def missing(members):
            members["senate-county.txt"] = b"\n".join(members["senate-county.txt"].splitlines()[5:])

        result = self.parse("MN", missing)
        self.assertEqual(result["outcome"], "validation_failed")

    def test_mn_media_bundle_needs_no_web_index_and_rejects_html_data(self):
        def media_only(members):
            members.pop("election.html")

        result = self.parse("MN", media_only)
        self.assertEqual(result["outcome"], "parsed", result)
        self.assertEqual(result["county_counts"], {"MN-SEN": 87, "MN-01": 21})

        def blocked_data(members):
            media_only(members)
            members["senate.txt"] = b"<html><title>Radware Captcha Page</title></html>"

        result = self.parse("MN", blocked_data)
        self.assertEqual(result["outcome"], "validation_failed")
        self.assertIn("received HTML", result["error"])

    def test_mn_legacy_captcha_cannot_be_replayed_as_valid_results(self):
        def blocked_index(members):
            members["election.html"] = (
                b"<html><title>Radware Captcha Page</title>Tuesday, November 3, 2026</html>"
            )

        result = self.parse("MN", blocked_index)
        self.assertEqual(result["outcome"], "validation_failed")
        self.assertIn("CAPTCHA", result["error"])

    def test_mn_election_paths_and_official_host_are_required(self):
        source = self.sources["mn_2026_general"]
        for bad in (
            replace(source, election_date="2024-11-05"),
            replace(source, url=source.url.replace("20261103", "20241105")),
            replace(
                source, url=source.url.replace("electionresultsfiles.sos.mn.gov", "example.com")
            ),
            replace(
                source,
                files=tuple(
                    (name, url.replace("20261103", "20241105")) for name, url in source.files
                ),
            ),
        ):
            with self.subTest(url=bad.url), self.assertRaisesRegex(ValueError, "election-dated"):
                mn.validate_source(bad)

    def test_mn_collector_fetches_exactly_five_data_files(self):
        source = self.sources["mn_2026_general"]
        members = self.members("MN")
        by_url = {
            url: members[name]
            for name, url in (*source.files, (source.primary_filename, source.url))
        }

        def fetch(source, settings, path, headers, *, url):
            body = by_url[url]
            path.write_bytes(body)
            return FetchResponse(200, {}, url, len(body), hashlib.sha256(body).hexdigest())

        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch) as request:
            path = self.root / "media.zip"
            bundles.fetch_bundle(source, self.settings, path)
            self.assertEqual(request.call_count, 5)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), set(source.expected_members))
                self.assertNotIn("election.html", archive.namelist())

    def test_each_source_rejects_duplicates_missing_races_and_wrong_elections(self):
        for code in ("MN", "NE", "TX"):

            def duplicate(members):
                name = state_adapter(code).source_id
                self.assertTrue(name)
                if code == "MN":
                    members["senate.txt"] += members["senate.txt"].splitlines(keepends=True)[0]
                elif code == "NE":
                    members["results.csv"] += members["results.csv"].splitlines(keepends=True)[1]
                else:
                    members["results.html"] *= 2

            self.assertEqual(self.parse(code, duplicate)["outcome"], "validation_failed", code)

            def wrong(members):
                key = "results.html" if code == "TX" else "election.html"
                members[key] = members[key].replace(b"2026", b"2024")

            self.assertEqual(self.parse(code, wrong)["outcome"], "validation_failed", code)

            def missing(members):
                if code == "MN":
                    members["senate.txt"] = b""
                elif code == "NE":
                    members["results.csv"] = members["results.csv"].replace(
                        b"For Governor and Lt. Governor", b"Other"
                    )
                else:
                    members["results.html"] = members["results.html"].replace(
                        b">GOVERNOR<", b">OTHER<"
                    )

            self.assertEqual(self.parse(code, missing)["outcome"], "validation_failed", code)

    def test_ne_only_allows_one_empty_trailing_column_and_literal_precinct_expression(self):
        for old, new in [
            (b',="0/1322",', b",=2+2,"),
            (b",1,0,0,", b",1,-1,0,"),
            (b'"SW"', b'"CTY"'),
            (b'1322",', b'1322",EXTRA,'),
        ]:

            def mutate(members):
                members["results.csv"] = members["results.csv"].replace(old, new, 1)

            self.assertEqual(self.parse("NE", mutate)["outcome"], "validation_failed")

    def test_tx_blank_zero_numeric_and_malformed_counts_remain_distinct(self):
        def numeric(members, value=b"0"):
            members["results.html"] = members["results.html"].replace(
                b'<td class="ballot" />', b'<td class="ballot">' + value + b"</td>"
            )

        zero = self.parse("TX", numeric)
        self.assertTrue(zero["all_counts_zero"])
        positive = self.parse("TX", lambda m: numeric(m, b"1,234"))
        self.assertEqual(positive["outcome"], "parsed", positive)
        for value in (b"1,23", b"-1", b"NaN", b"1.5"):
            result = self.parse("TX", lambda m: numeric(m, value))
            self.assertEqual(result["outcome"], "validation_failed")

        def mixed(members):
            members["results.html"] = members["results.html"].replace(
                b'<td class="ballot" />', b'<td class="ballot">0</td>', 1
            )

        self.assertEqual(self.parse("TX", mixed)["outcome"], "validation_failed")

    def test_new_primary_names_are_pinned_and_deterministic(self):
        source = self.sources["ne_2026_general"]

        def fetch(source, settings, path, headers, *, url):
            body = b"source body"
            path.write_bytes(body)
            return FetchResponse(200, {}, url, len(body), hashlib.sha256(body).hexdigest())

        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            a, b = self.root / "a.zip", self.root / "b.zip"
            self.assertEqual(
                bundles.fetch_bundle(source, self.settings, a).sha256,
                bundles.fetch_bundle(source, self.settings, b).sha256,
            )
            with zipfile.ZipFile(a) as archive:
                self.assertEqual(set(archive.namelist()), {"election.html", "results.csv"})
        with self.assertRaises(ValueError):
            replace(source, primary_filename="../bad")
        with self.assertRaises(ValueError):
            replace(source, files=(("results.csv", source.url),))

    def test_dashboard_supports_unknown_totals_and_aggregate_only_states(self):
        self.assertEqual(party_group("DFL", "Example"), "DEM")
        for code in ("MN", "NE", "TX"):
            report = self.parse(code)
            directory = Path(report["output_dir"])

            def read(name):
                with (directory / name).open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                for row in rows:
                    row["total_votes"] = int(row["total_votes"]) if row["total_votes"] else None
                return rows

            payload = make_payload(
                json.loads((STATIC / (code.lower() + "-map.json")).read_text(encoding="utf-8")),
                {"status": "test"},
                read("county_results.csv"),
                read("contest_results.csv"),
                state=code,
            )
            self.assertEqual(payload["county_detail"], code == "MN")
            self.assertEqual(
                payload["races"][code + "-SEN"]["stats"]["total_votes"]["total"],
                None if code == "TX" else 0,
            )
            self.assertEqual(
                len(publication.targets({"state": code, "dataset": "election_results"})),
                2 if code == "MN" else 1,
            )


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class ExpansionDatabaseTests(unittest.TestCase):
    def test_all_three_state_loads_publications_reader_permissions_and_replay(self):
        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        db = {**connection_settings(), "dbname": name}
        with connect(db) as conn:
            for relation, migration in [
                ("ops.sources", "001_initial.sql"),
                ("serving.publications", "002_publication.sql"),
                ("ky.results", "003_state_schemas.sql"),
                ("tx.results", "004_mn_ne_tx.sql"),
            ]:
                if conn.execute("SELECT to_regclass(%s) AS t", (relation,)).fetchone()["t"] is None:
                    conn.execute((ROOT / "migrations" / migration).read_text(encoding="utf-8"))
            conn.execute("TRUNCATE ops.sources CASCADE")
        for code in ("MN", "NE", "TX"):
            f = ExpansionTests()
            f.setUp()
            self.addCleanup(f.doCleanups)
            report = f.parse(code)
            loaded = load_parsed(f.source, f.settings, f.root, Path(report["output_dir"]), db)
            self.assertEqual(loaded["outcome"], "succeeded", loaded)
            candidate = publication.prepare(ROOT, db, loaded["release_id"], "test")
            self.assertEqual(candidate["outcome"], "validated")
            with connect(db) as conn:
                self.assertIsNone(
                    conn.execute(
                        "SELECT publication_id FROM serving.current_publications "
                        "WHERE source_id=%s",
                        (f.source.source_id,),
                    ).fetchone()
                )
            published = publication.publish_prepared(ROOT, db, candidate["candidate_id"])
            self.assertEqual(published["outcome"], "published")
            repeated = publication.publish(
                ROOT, db, loaded["release_id"], "test", skip_current=True
            )
            self.assertEqual(repeated["outcome"], "already_current")
            with connect(db) as conn:
                conn.execute("SET ROLE election_dashboard")
                self.assertTrue(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (code.lower() + ".contest_candidate_results",),
                    ).fetchone()["ok"]
                )
                self.assertFalse(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (code.lower() + ".results",),
                    ).fetchone()["ok"]
                )
