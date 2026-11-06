"""Virginia, Idaho, Washington, Colorado, Iowa, Oregon, California, Maryland and Hawaii.

Each fixture is a bundle captured by the production collector from the state's official
results site; ``tests/fixtures/nine_states/provenance.json`` records every component URL
and hash. Adversarial cases edit temporary copies.
"""

import csv
import gzip
import hashlib
import importlib
import io
import json
import os
import re
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

from election_pipeline import bundles, publication
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors import clarity as clarity_collector
from election_pipeline.collectors import enhanced_voting, enr_export
from election_pipeline.dashboard.server import STATIC, make_payload, national_payload
from election_pipeline.database import (
    connect,
    connection_settings,
    converted_rows,
    load_parsed,
    verify,
)
from election_pipeline.deployment import migrations
from election_pipeline.parsers import clarity, ia, md, totals
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/nine_states"
LEDGER = json.loads((FIXTURES / "provenance.json").read_text())
CODES = ("VA", "ID", "WA", "CO", "IA", "OR", "CA", "MD", "HI")
MIGRATIONS = dict(zip(CODES, range(14, 23)))
HOSTS = {
    "VA": "enr.elections.virginia.gov",
    "ID": "results.voteidaho.gov",
    "WA": "results.votewa.gov",
    "CO": "results.enr.clarityelections.com",
    "IA": "results.enr.clarityelections.com",
    "OR": "results.oregonvotes.gov",
    "CA": "api.sos.ca.gov",
    "MD": "elections.maryland.gov",
    "HI": "elections.hawaii.gov",
}
OREGON = importlib.import_module("election_pipeline.parsers.or")  # "or" is a keyword


def members(code):
    with zipfile.ZipFile(FIXTURES / f"{code.lower()}.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def edit_json(data, name, change):
    value = json.loads(data[name])
    change(value)
    data[name] = json.dumps(value).encode()


def english(values):
    return next(v["text"] for v in values if v.get("languageId") == "en")


class NineStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")

    def parse(self, code, mutate=None):
        self.source = self.sources[code.lower() + "_2026_general"]
        data = members(code)
        if mutate:
            mutate(data)
        raw = self.root / "input.zip"
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.store = ArchiveStore(self.root, self.source)
        target = self.store.archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        methods = state_adapter(self.source.state).methods
        for row in rows:
            for field in methods:
                row[field] = int(row[field]) if row[field] else None
        return rows

    def payload(self, code, report):
        geography = json.loads((STATIC / (code.lower() + "-map.json")).read_text())
        return make_payload(
            geography,
            {"status": "unofficial"},
            self.read(report, "county_results.csv"),
            self.read(report, "contest_results.csv"),
            state=code,
        )

    def failed(self, code, mutate, message=None):
        report = self.parse(code, mutate)
        self.assertEqual(report["outcome"], "validation_failed", report)
        if message:
            self.assertIn(message, json.dumps(report))
        return report

    # Shared contract -------------------------------------------------------------------

    def test_fixture_provenance_rosters_and_configuration_match(self):
        project = yaml.safe_load((ROOT / "dbt/dbt_project.yml").read_text())
        self.assertEqual(set(LEDGER), set(CODES))
        for code in CODES:
            entry = LEDGER[code]
            raw = (FIXTURES / f"{code.lower()}.zip").read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["archive_sha256"], code)
            data = members(code)
            source = self.sources[code.lower() + "_2026_general"]
            self.assertEqual(set(data), set(source.expected_members), code)
            for component in entry["components"]:
                self.assertEqual(component["url"].split("/")[2], HOSTS[code], code)
                if component["name"] in data:
                    body = data[component["name"]]
                    self.assertEqual(hashlib.sha256(body).hexdigest(), component["sha256"])
            self.assertEqual(set(state_adapter(code).races), set(ROSTERS[code]), code)
            self.assertEqual(set(project["vars"][code.lower() + "_races"]), set(ROSTERS[code]))
            self.assertEqual(
                project["vars"].get(code.lower() + "_methods", []),
                list(state_adapter(code).methods[1:]),
            )
        # No race is invented for offices these states do not elect in 2026.
        for code, race in (("WA", "WA-SEN"), ("WA", "WA-GOV"), ("VA", "VA-GOV"), ("CA", "CA-SEN")):
            self.assertNotIn(race, state_adapter(code).races)
        for code in ("MD", "HI"):
            self.assertNotIn(code + "-SEN", state_adapter(code).races)

    def test_official_snapshots_parse_verify_reconcile_convert_and_replay(self):
        expected = {
            "VA": (1010, 12, {"VA-SEN": 133, "VA-11": 2}),
            "ID": (665, 4, {"ID-SEN": 44, "ID-GOV": 44, "ID-01": 19}),
            "WA": (180, 10, {"WA-03": 7, "WA-07": 1}),
            "CO": (1201, 10, {"CO-SEN": 64, "CO-GOV": 64, "CO-05": 1}),
            "IA": (1076, 6, {"IA-SEN": 99, "IA-GOV": 99, "IA-04": 36}),
            "OR": (510, 8, {"OR-SEN": 36, "OR-GOV": 36, "OR-02": 22}),
            "CA": (106, 53, {}),
            "MD": (282, 9, {"MD-GOV": 24, "MD-01": 11}),
            "HI": (8, 3, {}),
        }
        for code, (records, contests, counties) in expected.items():
            with self.subTest(code=code):
                report = self.parse(code)
                self.assertEqual(report["outcome"], "parsed", report)
                self.assertEqual(report["output_rows"], records)
                self.assertEqual(report["contest_count"], contests)
                self.assertFalse(report["all_counts_zero"])
                self.assertEqual(
                    report["county_reconciliation"],
                    "passed" if state_adapter(code).county_results else "not_available",
                )
                for race, number in counties.items():
                    self.assertEqual(report["county_counts"][race], number)
                directory = Path(report["output_dir"])
                verify(self.source, self.settings, self.store, directory)
                contract = state_adapter(code).contract("election_results")
                converted = list(
                    converted_rows(
                        directory / contract.filename, contract.fields, self.source, 88, records
                    )
                )
                self.assertEqual(converted[0][:2], (88, 2))
                self.assertEqual(len(converted[0]), len(contract.fields) - 1)
                replay = parse_source(
                    self.source, self.settings, self.root, sha256=report["identity"]["raw_sha256"]
                )
                self.assertEqual(replay["outcome"], "already_parsed")

    def test_maps_and_dashboard_match_all_new_state_results(self):
        providers = {}
        shapes = {
            "VA": (133, 11),
            "ID": (44, 2),
            "WA": (39, 10),
            "CO": (64, 8),
            "IA": (99, 4),
            "OR": (36, 6),
            "CA": (58, 52),
            "MD": (24, 8),
            "HI": (5, 2),
        }
        for code, (counties, districts) in shapes.items():
            with self.subTest(code=code):
                geography = json.loads((STATIC / (code.lower() + "-map.json")).read_text())
                self.assertEqual(len(geography["counties"]), counties)
                self.assertEqual(len(geography["districts"]), districts)
                self.assertEqual(
                    len({c["id"] for c in geography["counties"]}), counties, "unique county keys"
                )
                payload = self.payload(code, self.parse(code))
                adapter = state_adapter(code)
                self.assertEqual(set(payload["races"]), set(adapter.races))
                self.assertEqual(payload["county_detail"], adapter.county_results)
                self.assertEqual(payload["methods"], adapter.methods)
                for race in payload["races"].values():
                    stats = race["stats"]
                    self.assertGreater(stats["total_votes"]["total"], 0)
                    if len(adapter.methods) > 1:
                        self.assertEqual(
                            sum(stats[m]["total"] for m in adapter.methods[1:]),
                            stats["total_votes"]["total"],
                        )
                for race in (code + "-SEN", code + "-GOV"):
                    if race in payload["races"] and adapter.county_results:
                        self.assertEqual(len(payload["races"][race]["counties"]), counties)
                providers[code] = lambda payload=payload: payload
        self.assertEqual(set(national_payload(providers)["states"]), set(CODES))

    def test_each_migration_is_additive_and_scoped_to_one_state(self):
        for code, number in MIGRATIONS.items():
            schema = code.lower()
            path = ROOT / f"migrations/{number:03}_{schema}.sql"
            body = migrations.validate_migration(path.read_text(), schema)
            quoted = f'"{schema}"' if schema == "or" else schema
            self.assertIn(f"CREATE TABLE {quoted}.results", body)
            self.assertEqual(
                f"{quoted}.county_candidate_results" in body, state_adapter(code).county_results
            )
            for method in state_adapter(code).methods[1:]:
                self.assertIn(f"{method} bigint NOT NULL", body)

    def test_vote_method_contract_matches_each_state(self):
        with self.assertRaises(ValueError):
            totals.with_methods(("total_votes",))
        with self.assertRaises(ValueError):
            totals.with_methods(("mail", "mail"))
        report = self.parse("VA")
        rows = self.read(report, "results.csv")
        for row in rows:
            self.assertEqual(
                sum(row[m] for m in state_adapter("VA").methods[1:]), row["total_votes"]
            )
        senate = [
            r for r in self.read(report, "contest_results.csv") if r["contest_id"] == "VA-SEN"
        ]
        self.assertGreater(sum(r["early_voting"] for r in senate), 0)
        self.assertEqual(report["vote_methods"], "stored")
        # A parser that stores different methods than its state publishes is refused.
        with patch("election_pipeline.parsers.ia.METHODS", ("absentee_combined", "election_day")):
            self.failed("IA", None, "Stored vote methods differ")

    # Enhanced Voting (Virginia, Idaho, Washington) -----------------------------------------

    def test_ev_altered_county_count_or_method_split_rejected(self):
        def county(data):
            def change(item):
                option = item["breakdownResults"][0]["ballotOptions"][0]
                option["voteCount"] += 1
                item["breakdownResults"][0]["voteTotal"] += 1
                option["groupResults"][0]["voteCount"] += 1

            edit_json(data, "item-va-sen.json", change)

        self.failed("VA", county, "County and official contest candidate totals disagree")

        def split(data):
            def change(item):
                groups = item["breakdownResults"][0]["ballotOptions"][0]["groupResults"]
                source = next(g for g in groups if g["voteCount"] > 0)
                target = next(g for g in groups if g is not source)
                source["voteCount"] -= 1
                target["voteCount"] += 1

            edit_json(data, "item-va-sen.json", change)

        # Moving one vote between methods keeps every total equal, but county methods then
        # no longer add up to the statewide methods.
        self.failed("VA", split, "County and official contest candidate totals disagree")

        def unknown_method(data):
            def change(item):
                group = item["breakdownResults"][0]["ballotOptions"][0]["groupResults"][0]
                for name in group["groupName"]:
                    name["text"] = "Curbside"

            edit_json(data, "item-va-sen.json", change)

        self.failed("VA", unknown_method, "vote methods differ")

    def test_ev_wrong_election_contest_or_unexpected_methods_rejected(self):
        self.failed(
            "VA",
            lambda d: edit_json(
                d, "data.json", lambda v: v["election"].update(electionDate="2025-11-04")
            ),
        )
        self.failed(
            "ID",
            lambda d: edit_json(d, "data.json", lambda v: v["election"].update(isProduction=False)),
        )

        def other_contest(data):
            data["item-wa-01.json"] = data["item-wa-02.json"]

        self.failed("WA", other_contest, "another contest")

        def methods_on_totals_state(data):
            def change(item):
                option = item["breakdownResults"][0]["ballotOptions"][0]
                option["groupResults"] = [
                    {"groupName": [{"languageId": "en", "text": "Election Day"}], "voteCount": 1}
                ]

            edit_json(data, "item-id-sen.json", change)

        self.failed("ID", methods_on_totals_state, "unexpected vote-method detail")

        def dropped_county(data):
            def change(item):
                item["breakdownResults"].pop()
                item["breakdownResultCount"] -= 1

            edit_json(data, "item-id-gov.json", change)

        self.failed("ID", dropped_county)

    def ev_server(self, code, calls, changes=None):
        source = self.sources[code.lower() + "_2026_general"]
        data = members(code)
        items = {}
        for member in source.supplemental_members:
            items[json.loads(data[member])["id"]] = member
        served = {"data": 0}

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            self.assertIsNone(form)
            calls.append(url)
            if url == source.url:
                served["data"] += 1
                body = data["data.json"]
                if changes and served["data"] > 1:
                    body = body.replace(b'"asOf"', b'"asOf" ', 1)
            else:
                identity = url.rsplit("/", 1)[1]
                self.assertEqual(url, enhanced_voting.item_url(source.url, identity))
                body = data[items[identity]]
            path.write_bytes(body)
            return FetchResponse(
                200,
                {"content-type": "application/json; charset=utf-8"},
                url,
                len(body),
                hashlib.sha256(body).hexdigest(),
            )

        return source, fetch, data

    def test_ev_collector_fetches_only_selected_items_and_rejects_mid_update(self):
        for code in ("VA", "ID", "WA"):
            with self.subTest(code=code):
                calls = []
                source, fetch, data = self.ev_server(code, calls)
                destination = self.root / f"{code}.zip"
                with patch(
                    "election_pipeline.collectors.enhanced_voting.fetch_file", side_effect=fetch
                ):
                    bundles.fetch_bundle(source, self.settings, destination)
                    with zipfile.ZipFile(destination) as archive:
                        self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)
                    self.assertEqual(len(calls), len(source.supplemental_members) + 2)
                    calls.clear()
                    bundles.fetch_bundle(
                        source, self.settings, self.root / "again.zip", previous=destination
                    )
                    self.assertEqual(calls, [source.url])  # unchanged: previous bundle reused
        calls = []
        source, fetch, _ = self.ev_server("ID", calls, changes=True)
        with patch("election_pipeline.collectors.enhanced_voting.fetch_file", side_effect=fetch):
            with self.assertRaisesRegex(FetchFailure, "changed during collection"):
                bundles.fetch_bundle(source, self.settings, self.root / "x.zip")
        for bad in ("../data", "01000000-70f4-ca87-27ba-08df23e9954c?x=1", "abc"):
            with self.assertRaises(ValueError):
                enhanced_voting.item_url(source.url, bad)
        with self.assertRaises(ValueError):
            enhanced_voting.item_url(
                "https://evil.example/x/data", "0" * 8 + "-0000-0000-0000-" + "0" * 12
            )

    # Clarity (Colorado, Iowa) --------------------------------------------------------------

    def test_co_county_edit_and_summary_mismatch_rejected(self):
        def county(data):
            raw = clarity.detail_member(data["detail.zip"], self.settings, "xml")
            raw = re.sub(
                rb'(<County name="Adams" votes=")(\d+)',
                lambda m: m[1] + str(int(m[2]) + 1).encode(),
                raw,
                count=1,
            )
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("detail.xml", raw)
            data["detail.zip"] = buffer.getvalue()

        self.failed("CO", county)
        self.failed(
            "CO",
            lambda d: d.update(
                {"summary.json": d["summary.json"].replace(b"[ 920511,", b"[ 920512,")}
            ),
        )
        report = self.parse("CO")
        names = {r["choice"] for r in self.read(report, "results.csv")}
        self.assertIn('Mark "Marky Jr" Elworth', names)  # detail spelling, quote styles folded

    def test_clarity_json_may_arrive_gzipped_and_settings_must_match_the_state(self):
        raw = members("IA")["settings.json"]
        self.assertEqual(
            clarity.settings_check(
                gzip.compress(clarity.document(raw, self.settings)), "IA", self.settings
            ),
            clarity.settings_check(raw, "IA", self.settings),
        )
        with self.assertRaises(ValueError):
            clarity.settings_check(raw, "CO", self.settings)

    def test_ia_blank_cells_only_where_total_is_zero(self):
        self.assertEqual(ia.group("", "", "0"), (0, 0, 0))
        self.assertEqual(ia.group("3", "0", "3"), (3, 0, 3))
        for bad in (("", "", "5"), ("", "2", "2"), ("1", "1", "")):
            with self.assertRaises(ValueError):
                ia.group(*bad)
        with self.assertRaises(ValueError):
            ia.columns("Adair" + " " * 26 + "5167", 3)  # value not aligned to its column

        def totals_row(data):
            raw = clarity.detail_member(data["detail.zip"], self.settings, "txt")
            raw = raw.replace(
                b"Totals:                       2178374",
                b"Totals:                       2178375",
                1,
            )
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("detail.txt", raw)
            data["detail.zip"] = buffer.getvalue()

        report = self.parse("IA", totals_row)
        self.assertEqual(report["outcome"], "parsed", report)  # registered voters are not stored

        def candidate_total(data):
            raw = clarity.detail_member(data["detail.zip"], self.settings, "txt")
            line = re.search(rb"\nAdair {20,}5167 {20,}1275 {20,}645 {20,}1920", raw)[0]
            raw = raw.replace(line, line.replace(b"1920", b"1921"), 1)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("detail.txt", raw)
            data["detail.zip"] = buffer.getvalue()

        self.failed("IA", candidate_total)
        report = self.parse("IA")
        self.assertEqual(report["vote_methods"], "stored")
        county = [r for r in self.read(report, "county_results.csv") if r["contest_id"] == "IA-SEN"]
        self.assertEqual(
            sum(r["election_day"] + r["absentee_combined"] for r in county),
            sum(r["total_votes"] for r in county),
        )

    def test_clarity_collector_reads_only_the_current_version(self):
        source = self.sources["co_2026_general"]
        data = members("CO")
        version = data["current_ver.txt"].decode().strip()
        calls = []

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            calls.append(url)
            if url == source.url:
                body, kind = data["current_ver.txt"], "text/plain; charset=utf-8"
            else:
                self.assertIn(f"/127407/{version}/", url)
                name = {
                    v: k
                    for k, v in importlib.import_module(
                        "election_pipeline.parsers.co"
                    ).VERSION_FILES.items()
                }[url.split(f"/{version}/", 1)[1]]
                body = data[name]
                kind = "application/zip" if name.endswith(".zip") else "application/json"
            path.write_bytes(body)
            return FetchResponse(
                200, {"content-type": kind}, url, len(body), hashlib.sha256(body).hexdigest()
            )

        destination = self.root / "co.zip"
        with patch("election_pipeline.collectors.clarity.fetch_file", side_effect=fetch):
            bundles.fetch_bundle(source, self.settings, destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)
            calls.clear()
            bundles.fetch_bundle(source, self.settings, self.root / "b.zip", previous=destination)
            self.assertEqual(calls, [source.url])
            old = time.time() - enr_export.REFRESH_SECONDS - 5
            os.utime(destination, (old, old))
            calls.clear()
            bundles.fetch_bundle(source, self.settings, self.root / "c.zip", previous=destination)
            self.assertEqual(len(calls), 4)
        for bad in (b"12a", b"", b"1/2"):
            with self.assertRaises(ValueError):
                clarity_collector.version_base(source.url, bad)
        with self.assertRaises(ValueError):
            clarity_collector.version_base("https://evil.example/CO/1/current_ver.txt", b"5")

    # Oregon --------------------------------------------------------------------------------

    def test_or_county_file_must_match_the_statewide_export(self):
        def stranger(data):
            data["county-baker.csv"] = data["county-baker.csv"].replace(
                b'"Jeff Merkley"', b'"Jeff Merkely"', 1
            )

        self.failed("OR", stranger, "candidate differs from statewide file")

        def county_votes(data):
            data["county-lane.csv"] = re.sub(
                rb'("Jeff Merkley",[^\n]*?,1,)(\d+)',
                lambda m: m[1] + str(int(m[2]) + 1).encode(),
                data["county-lane.csv"],
                count=1,
            )

        self.failed("OR", county_votes, "County and official contest candidate totals disagree")
        self.failed(
            "OR",
            lambda d: d.update(
                {"export.html": d["export.html"].replace(b"November 3, 2026", b"May 19, 2026")}
            ),
        )
        report = self.parse("OR")
        parties = {r["choice"]: r["choice_party"] for r in self.read(report, "contest_results.csv")}
        self.assertEqual(parties["Christine Drazan"], "REP")  # "Republican, Libertarian"
        self.assertEqual(parties["Chris Henry"], "PROGRESSIVE")

    def test_or_exports_choose_the_pages_own_csv_option(self):
        page = members("OR")["export.html"].decode("utf-8", errors="replace")
        (self.root / "export.html").write_text(page, encoding="utf-8")
        plan = OREGON.exports({"export.html": self.root / "export.html"}, None)
        self.assertEqual(len(plan), 38)
        self.assertTrue(all(entry[3] == OREGON.CSV_OPTION for entry in plan))
        url = "https://results.oregonvotes.gov/ResultsExport.aspx"
        _, form = enr_export.postback(page, url, plan[0][2], OREGON.CSV_OPTION)
        self.assertIn(b"ctl00%24MainContent%24rblTypes=1", form)
        for choice in ({"ctl00$MainContent$rblTypes": "9"}, {"ctl00$MainContent$other": "1"}):
            with self.assertRaises(ValueError):
                enr_export.postback(page, url, plan[0][2], choice)

    # California, Maryland, Hawaii --------------------------------------------------------

    def test_ca_documents_must_be_the_named_contest_with_exact_counts(self):
        self.failed(
            "CA",
            lambda d: d.update({"us-rep-07.json": d["us-rep-08.json"]}),
            "belongs to another contest",
        )

        def sloppy(data):
            value = json.loads(data["governor.json"])
            value["candidates"][0]["Votes"] = "5,68,1264"
            data["governor.json"] = json.dumps(value).encode()

        self.failed("CA", sloppy, "Invalid formatted vote count")

    def test_md_totals_and_jurisdiction_names(self):
        self.assertEqual(md.county("Saint Mary's"), "St. Mary's County")
        self.assertEqual(md.county("Baltimore City"), "Baltimore City")
        self.assertEqual(md.county("Allegany"), "Allegany County")

        def county(data):
            data["house-01.html"] = data["house-01.html"].replace(b">9,598<", b">9,599<", 1)

        self.failed("MD", county, "county rows do not add up to the Totals row")
        self.failed(
            "MD",
            lambda d: d.update({"house-02.html": d["house-03.html"]}),
            "belongs to another district",
        )

    def test_hi_methods_and_tickets(self):
        report = self.parse("HI")
        rows = self.read(report, "contest_results.csv")
        governor = {r["choice"] for r in rows if r["contest_id"] == "HI-GOV"}
        self.assertIn("GREEN, Josh / KAWAKAMI, Derek S.K.", governor)
        for row in rows:
            self.assertEqual(row["mail"] + row["in_person"], row["total_votes"])

        def split(data):
            data["summary.txt"] = data["summary.txt"].replace(
                b"\t123016\t3006\t", b"\t123017\t3006\t", 1
            )

        self.failed("HI", split, "Vote methods do not add up")


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class NineStateDatabaseTests(unittest.TestCase):
    def test_nine_states_load_validate_publish_and_enforce_reader_permissions(self):
        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        db = {**connection_settings(), "dbname": name}
        with connect(db) as conn:
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                schema = {
                    "001_initial.sql": "ops.sources",
                    "002_publication.sql": "serving.publications",
                    "003_state_schemas.sql": "ky.results",
                    "004_mn_ne_tx.sql": "tx.results",
                }.get(path.name, f'"{path.stem[4:]}".results')
                if conn.execute("SELECT to_regclass(%s) AS t", (schema,)).fetchone()["t"] is None:
                    conn.execute(path.read_text())
            conn.execute("TRUNCATE ops.sources CASCADE")
        for code in CODES:
            with self.subTest(code=code):
                f = NineStateTests()
                f.setUp()
                self.addCleanup(f.doCleanups)
                report = f.parse(code)
                loaded = load_parsed(f.source, f.settings, f.root, Path(report["output_dir"]), db)
                self.assertEqual(loaded["outcome"], "succeeded", loaded)
                candidate = publication.prepare(ROOT, db, loaded["release_id"], "test")
                self.assertEqual(candidate["outcome"], "validated", candidate)
                self.assertEqual(
                    publication.publish_prepared(ROOT, db, candidate["candidate_id"])["outcome"],
                    "published",
                )
                schema = f'"{code.lower()}"'
                with connect(db) as conn:
                    conn.execute("SET ROLE election_dashboard")
                    for table, allowed in (("contest_candidate_results", True), ("results", False)):
                        self.assertEqual(
                            conn.execute(
                                "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                                (f"{schema}.{table}",),
                            ).fetchone()["ok"],
                            allowed,
                        )
                    conn.execute("RESET ROLE")
                    current = (
                        "WHERE publication_id=(SELECT publication_id FROM "
                        "serving.current_publications WHERE source_id=%s)"
                    )
                    methods = state_adapter(code).methods
                    contest = conn.execute(
                        f"SELECT count(*) AS n, sum(total_votes) AS total, "
                        f"sum({' + '.join(methods[1:]) or 'total_votes'}) AS methods "
                        f"FROM {schema}.contest_candidate_results {current}",
                        (f.source.source_id,),
                    ).fetchone()
                    self.assertGreater(contest["n"], 0)
                    self.assertEqual(contest["total"], contest["methods"])
                    if state_adapter(code).county_results:
                        county = conn.execute(
                            f"SELECT count(*) AS n FROM {schema}.county_candidate_results "
                            + current,
                            (f.source.source_id,),
                        ).fetchone()["n"]
                        self.assertGreater(county, 0)


if __name__ == "__main__":
    unittest.main()
