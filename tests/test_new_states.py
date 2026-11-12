"""South Dakota, New Mexico and Florida: recorded official responses and adversarial edits.

The fixtures are exact official bytes captured by the production collector; see
``tests/fixtures/new_states/provenance.json``. Mutations happen in temporary copies.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
import zipfile
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs

from loading import copies, migrated, requires_database

from election_pipeline import bundles, publication
from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file
from election_pipeline.collectors import enr_export
from election_pipeline.dashboard.server import STATIC, make_payload, national_payload
from election_pipeline.database import (
    connect,
    load_parsed,
)
from election_pipeline.parsers.enr_platform import export_target
from election_pipeline.parsers.workbook import Workbook
from election_pipeline.parsing import parse_source
from election_pipeline.settings import CollectorSettings, Source, load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/new_states"
CODES = ("SD", "NM", "FL")
LEDGER = json.loads((FIXTURES / "provenance.json").read_text())
LIMITS = SimpleNamespace(max_uncompressed_bytes=10**9, max_csv_rows=10**6)


def members(code):
    return {
        entry["member"]: gzip.decompress((FIXTURES / (name + ".gz")).read_bytes())
        for name, entry in LEDGER.items()
        if name.startswith(code.lower() + "-")
    }


def sheet_member(raw, sheet):
    """Worksheet XML path for a sheet name."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        book = archive.read("xl/workbook.xml").decode()
        rels = archive.read("xl/_rels/workbook.xml.rels").decode()
    identity = re.search(rf'name="{re.escape(sheet)}"[^>]*r:id="(\w+)"', book)[1]
    target = re.search(rf'Id="{identity}"[^>]*Target="([^"]+)"', rels) or re.search(
        rf'Target="([^"]+)"[^>]*Id="{identity}"', rels
    )
    return "xl/" + target[1].lstrip("/").removeprefix("xl/")


def set_cells(raw, sheet, values):
    """Rewrite cells (e.g. {"C8": "5"}) of one sheet as inline strings."""
    member = sheet_member(raw, sheet)
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as original,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == member:
                text = data.decode("utf-8")
                for ref, value in values.items():
                    cell = re.compile(rf'<x:c r="{ref}"[^>]*?(?:/>|>.*?</x:c>)')
                    replacement = (
                        f'<x:c r="{ref}" t="inlineStr"><x:is><x:t>{value}</x:t></x:is></x:c>'
                    )
                    text, count = cell.subn(replacement, text)
                    assert count == 1, ref
                data = text.encode("utf-8")
            target.writestr(item.filename, data)
    return out.getvalue()


def locate(raw, sheet, label):
    """Row number and candidate columns for a county or TOTALS row."""
    rows = Workbook(raw, LIMITS).rows(sheet)
    header = next(r for r in rows if len(r) > 2 and r[1] == "County")
    number = next(i + 1 for i, r in enumerate(rows) if len(r) > 1 and r[1] == label)
    columns = {" ".join(str(n).split()): chr(ord("C") + i) for i, n in enumerate(header[2:])}
    return number, columns, rows[number - 1]


def relabel(raw, old, new):
    """Replace text in every XML part of a workbook, such as its sheets' title."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as original,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename.endswith(".xml"):
                data = data.replace(old, new)
            target.writestr(item.filename, data)
    return out.getvalue()


def certified(m):
    """A snapshot as its state would label it once certified: pages and workbooks."""
    for name, value in m.items():
        if name.endswith(".html"):
            m[name] = value.replace(b"Unofficial Results", b"Official Results")
        elif name.endswith(".xlsx"):
            m[name] = relabel(value, b"2026 Unofficial General", b"2026 Official General")


class NewStateTests(unittest.TestCase):
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
        for row in rows:
            row["total_votes"] = int(row["total_votes"]) if row["total_votes"] else None
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

    def failed(self, code, mutate):
        report = self.parse(code, mutate)
        self.assertEqual(report["outcome"], "validation_failed", report)
        return report

    # Shared contract -------------------------------------------------------------------

    def test_fixture_provenance_and_configuration_match(self):
        hosts = {
            "SD": "electionresults.sd.gov",
            "NM": "electionresults.sos.nm.gov",
            "FL": "flelectionfiles.floridados.gov",
        }
        for name, entry in LEDGER.items():
            raw = gzip.decompress((FIXTURES / (name + ".gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"], name)
            self.assertEqual(len(raw), entry["bytes"], name)
            self.assertEqual(entry["url"].split("/")[2], hosts[name[:2].upper()], name)
        for code in CODES:
            source = self.sources[code.lower() + "_2026_general"]
            self.assertEqual(set(members(code)), set(source.expected_members))
        self.assertNotIn("FL-10", state_adapter("FL").races)

    def test_official_snapshots_parse_verify_reconcile_convert_and_replay(self):
        for code, records, contests, counties in [
            ("SD", 402, 3, {"SD-SEN": 66, "SD-GOV": 66, "SD-AL": 66}),
            ("NM", 226, 5, {"NM-SEN": 33, "NM-GOV": 33, "NM-02": 14}),
            ("FL", 1248, 29, {"FL-SEN": 67, "FL-GOV": 67, "FL-02": 16}),
        ]:
            with self.subTest(code=code):
                report = self.parse(code)
                self.assertEqual(report["outcome"], "parsed", report)
                self.assertEqual(report["output_rows"], records)
                self.assertEqual(report["contest_count"], contests)
                self.assertFalse(report["all_counts_zero"])
                for race, number in counties.items():
                    self.assertEqual(report["county_counts"][race], number)
                directory = Path(report["output_dir"])
                converted = copies(self.source, self.settings, self.store, directory, 88)[
                    ("landing", "results")
                ]
                self.assertEqual(len(converted), records)
                self.assertEqual(converted[0][:2], (88, 2))
                replay = parse_source(
                    self.source, self.settings, self.root, sha256=report["identity"]["raw_sha256"]
                )
                self.assertEqual(replay["outcome"], "already_parsed")

    def test_maps_and_dashboard_match_all_new_state_results(self):
        providers = {}
        for code, counties, districts in [("SD", 66, 1), ("NM", 33, 3), ("FL", 67, 28)]:
            geography = json.loads((STATIC / (code.lower() + "-map.json")).read_text())
            self.assertEqual(len(geography["counties"]), counties)
            self.assertEqual(len(geography["districts"]), districts)
            payload = self.payload(code, self.parse(code))
            self.assertEqual(set(payload["races"]), set(state_adapter(code).races))
            self.assertTrue(payload["county_detail"])
            for race in (code + "-SEN", code + "-GOV"):
                self.assertGreater(payload["races"][race]["stats"]["total_votes"]["total"], 0)
                self.assertEqual(len(payload["races"][race]["counties"]), counties)
                self.assertNotIn("county_privacy_masked", payload["races"][race])
            providers[code] = lambda payload=payload: payload
        self.assertEqual(set(national_payload(providers)["states"]), set(CODES))

    # South Dakota -----------------------------------------------------------------------

    def sd_senate(self):
        book = Workbook(members("SD")["statewide.xlsx"], LIMITS)
        return next(s for s in book.sheets if s.endswith("United States Senator"))

    def test_sd_county_rows_and_quote_variants_reconcile_to_the_statewide_csv(self):
        report = self.parse("SD")
        self.assertEqual(report["county_reconciliation"], "passed")
        self.assertEqual(report["vote_methods"], [])
        rows = self.read(report, "county_results.csv")
        names = {r["choice"] for r in rows if r["contest_id"] == "SD-AL"}
        # The CSV writes 'Nikki' and the workbook "Nikki"; both are the same candidate.
        self.assertIn("Nicole 'Nikki' Gronli", names)
        self.assertIn("BROOKINGS", {r["county"] for r in rows})  # "(Vote Center)" removed

    def test_sd_altered_county_totals_csv_or_election_rejected(self):
        sheet = self.sd_senate()
        raw = members("SD")["statewide.xlsx"]
        row, columns, values = locate(raw, sheet, "Aurora")
        cell = columns["Mike Rounds"] + str(row)

        def county(m):
            m["statewide.xlsx"] = set_cells(m["statewide.xlsx"], sheet, {cell: values[3] + 1})

        def csv_votes(m):
            m["results.csv"] = m["results.csv"].replace(b",184589,", b",184590,")

        self.failed("SD", county)
        self.failed("SD", csv_votes)
        self.failed(
            "SD",
            lambda m: m.update(
                {"export.html": m["export.html"].replace(b"November 3, 2026", b"November 5, 2024")}
            ),
        )
        self.failed(
            "SD", lambda m: m.update({"results.csv": m["results.csv"].replace(b"2026", b"2024")})
        )

    def test_sd_page_without_an_update_time_parses_but_two_times_are_refused(self):
        shown = (
            b'<div class="last-updated"><span>Results last updated:</span>'
            b"<span>11/5/2026 6:45 PM CT</span></div>"
        )
        # As South Dakota's page has been since Nov 10, 2026: the box is empty.
        report = self.parse(
            "SD",
            lambda m: m.update(
                {
                    "export.html": m["export.html"].replace(
                        shown, b'<div class="last-updated"></div>'
                    )
                }
            ),
        )
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertIsNone(report["source_update_text"])
        second = shown.replace(b"6:45 PM", b"7:45 PM")
        self.failed(
            "SD",
            lambda m: m.update({"export.html": m["export.html"].replace(shown, shown + second)}),
        )

    def test_sd_and_nm_record_official_only_when_page_and_workbooks_all_say_so(self):
        for code in ("SD", "NM"):
            with self.subTest(code=code):
                self.assertIs(self.parse(code)["official_results"], False)
                self.assertIs(self.parse(code, certified)["official_results"], True)

                def pages_only(m):
                    for name in [n for n in m if n.endswith(".html")]:
                        m[name] = m[name].replace(b"Unofficial Results", b"Official Results")

                def unlabeled(m):
                    for name in [n for n in m if n.endswith(".html")]:
                        m[name] = m[name].replace(b"Unofficial Results", b"Certified Results")

                self.failed(code, pages_only)
                self.failed(code, unlabeled)

    def test_sd_consistent_downward_correction_is_valid(self):
        sheet = self.sd_senate()
        raw = members("SD")["statewide.xlsx"]
        row, columns, values = locate(raw, sheet, "Aurora")
        total_row, _, totals = locate(raw, sheet, "TOTALS")
        column = columns["Mike Rounds"]
        index = ord(column) - ord("A")

        def mutate(m):
            m["statewide.xlsx"] = set_cells(
                m["statewide.xlsx"],
                sheet,
                {f"{column}{row}": values[index] - 1, f"{column}{total_row}": totals[index] - 1},
            )
            m["results.csv"] = m["results.csv"].replace(
                f",{totals[index]},".encode(), f",{totals[index] - 1},".encode()
            )

        report = self.parse("SD", mutate)
        self.assertEqual(report["outcome"], "parsed", report)

    # New Mexico -------------------------------------------------------------------------

    def test_nm_privacy_masked_county_is_left_out_and_never_reconstructed(self):
        report = self.parse("NM")
        self.assertEqual(report["county_reconciliation"], "passed_except_privacy_masked_counties")
        self.assertEqual(report["privacy_masked_counties"], {"NM-02": ["CHAVES"]})
        self.assertEqual(set(report["vote_method_status"].values()), {"not_reported"})
        rows = self.read(report, "county_results.csv")
        district = [r for r in rows if r["contest_id"] == "NM-02"]
        self.assertNotIn("CHAVES", {r["county"] for r in district})
        official = {
            r["choice"]: r["total_votes"]
            for r in self.read(report, "contest_results.csv")
            if r["contest_id"] == "NM-02"
        }
        counted = {}
        for r in district:
            counted[r["choice"]] = counted.get(r["choice"], 0) + r["total_votes"]
        # Cunningham's printed Chaves count (65) is withheld with the rest of that county.
        self.assertEqual(official["GREGORY G CUNNINGHAM"] - counted["GREGORY G CUNNINGHAM"], 65)
        self.assertGreater(official["GABRIEL VASQUEZ"] - counted["GABRIEL VASQUEZ"], 0)
        payload = self.payload("NM", report)
        self.assertTrue(payload["races"]["NM-02"]["county_privacy_masked"])

    def nm_senate(self, edit):
        raw = members("NM")["county-nm-sen.xlsx"]
        sheet = next(s for s in Workbook(raw, LIMITS).sheets if s.startswith("Total"))
        row, columns, values = locate(raw, sheet, "Bernalillo")
        total_row, _, totals = locate(raw, sheet, "TOTALS")
        column = columns["BEN R LUJAN"]
        index = ord(column) - ord("A")
        cells = edit(column, row, int(values[index]), total_row, int(totals[index]))

        def mutate(m):
            m["county-nm-sen.xlsx"] = set_cells(m["county-nm-sen.xlsx"], sheet, cells)

        return mutate

    def test_nm_masked_senate_county_publishes_with_a_dashboard_note(self):
        masked = self.nm_senate(
            lambda c, r, value, t, total: {f"{c}{r}": "*", f"{c}{t}": total - value}
        )
        report = self.parse("NM", masked)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["privacy_masked_counties"]["NM-SEN"], ["BERNALILLO"])
        payload = self.payload("NM", report)
        self.assertTrue(payload["races"]["NM-SEN"]["county_privacy_masked"])
        self.assertNotIn("BERNALILLO", payload["races"]["NM-SEN"]["counties"])

    def test_nm_masks_that_do_not_add_up_and_altered_counts_rejected(self):
        # "*" without the matching drop in TOTALS, and TOTALS above the statewide CSV.
        self.failed("NM", self.nm_senate(lambda c, r, v, t, total: {f"{c}{r}": "*"}))
        self.failed(
            "NM",
            self.nm_senate(lambda c, r, v, t, total: {f"{c}{r}": "*", f"{c}{t}": total - v + 1}),
        )
        self.failed("NM", self.nm_senate(lambda c, r, v, t, total: {f"{c}{r}": v + 1}))
        self.failed(
            "NM", self.nm_senate(lambda c, r, v, t, total: {f"{c}{r}": v + 1, f"{c}{t}": total + 1})
        )
        self.failed("NM", self.nm_senate(lambda c, r, v, t, total: {f"{c}{r}": "-1"}))
        self.failed(
            "NM",
            lambda m: m.update({"fed.html": m["fed.html"].replace(b"November 3, 2026", b"2024")}),
        )
        self.failed(
            "NM",
            lambda m: m.update({"fed.csv": m["fed.csv"].replace(b"BEN R LUJAN", b"BEN LUJAN")}),
        )

    def test_nm_vote_methods_reconcile_when_new_mexico_fills_them(self):
        raw = members("NM")["county-nm-sen.xlsx"]
        sheets = list(Workbook(raw, LIMITS).sheets)
        total = next(s for s in sheets if s.startswith("Total"))
        rows = Workbook(raw, LIMITS).rows(total)

        def methods(shift):
            def mutate(m):
                data = m["county-nm-sen.xlsx"]
                for sheet in sheets:
                    if sheet == total:
                        continue
                    cells = {}
                    for number, values in enumerate(rows, 1):
                        if len(values) < 3 or values[1] in ("County", None, ""):
                            continue
                        for i, value in enumerate(values[2:]):
                            ref = f"{chr(ord('C') + i)}{number}"
                            if sheet.startswith("ElectionDay"):
                                cells[ref] = int(value) + shift
                            elif values[1] == "TOTALS":
                                cells[ref] = 0
                            else:
                                cells[ref] = 0
                    data = set_cells(data, sheet, cells)
                m["county-nm-sen.xlsx"] = data

            return mutate

        report = self.parse("NM", methods(0))
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["vote_method_status"]["NM-SEN"], "reported_not_stored")
        self.failed("NM", methods(1))

    def test_nm_export_controls_are_found_by_race_id(self):
        page = members("NM")["fed.html"].decode("utf-8", errors="replace")
        self.assertEqual(
            export_target(page, button_value="County Level", race_id="11185"),
            "ctl00$MainContent$rptRace$ctl00$CountyExport",
        )
        with self.assertRaises(ValueError):
            export_target(page, button_value="County Level", race_id="99999")
        sd = members("SD")["export.html"].decode("utf-8", errors="replace")
        self.assertEqual(
            export_target(sd, link_text="Statewide Export"), "ctl00$MainContent$Statewide"
        )

    # Florida ----------------------------------------------------------------------------

    def test_fl_tickets_write_ins_and_county_sums(self):
        report = self.parse("FL")
        self.assertEqual(report["statewide_totals"], "sum_of_official_county_rows")
        contests = self.read(report, "contest_results.csv")
        governor = {r["choice"]: r for r in contests if r["contest_id"] == "FL-GOV"}
        self.assertIn("Jolly / Graham", governor)
        self.assertIn("Donalds / Avila", governor)
        self.assertTrue(any(r["choice_party"] == "WRI" for r in governor.values()))
        self.assertIn("MIAMI-DADE", {r["county"] for r in self.read(report, "county_results.csv")})

    def test_fl_wrong_election_missing_duplicate_and_inconsistent_rows_rejected(self):
        lines = members("FL")["results.txt"].split(b"\r\n")
        senate = [i for i, line in enumerate(lines) if b"\tUSS\t" in line]

        def edit(change):
            def mutate(m):
                copy = list(lines)
                change(copy)
                m["results.txt"] = b"\r\n".join(copy)

            return mutate

        self.failed(
            "FL",
            edit(
                lambda c: c.__setitem__(
                    senate[0], c[senate[0]].replace(b"11/03/2026", b"11/05/2024")
                )
            ),
        )
        self.failed("FL", edit(lambda c: c.pop(senate[0])))
        self.failed("FL", edit(lambda c: c.insert(senate[0], c[senate[0]])))
        self.failed(
            "FL",
            edit(
                lambda c: c.__setitem__(
                    senate[0], re.sub(rb"\t63\t63\t", b"\t63\t62\t", c[senate[0]])
                )
            ),
        )
        self.failed("FL", edit(lambda c: c.__setitem__(senate[0], c[senate[0]] + b"\textra")))

    def test_fl_downward_correction_is_valid(self):
        def mutate(m):
            m["results.txt"] = m["results.txt"].replace(
                b"\tMoody\tAshley\t\t34791", b"\tMoody\tAshley\t\t34790", 1
            )

        report = self.parse("FL", mutate)
        self.assertEqual(report["outcome"], "parsed", report)


class Handler(BaseHTTPRequestHandler):
    seen = []

    def respond(self, body, content_type, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("Cookie"), None))
        self.respond(b"<form method='post'></form>", "text/html", "session=abc; Path=/")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append(("POST", self.path, self.headers.get("Cookie"), body))
        self.respond(b"PK-workbook", enr_export.WORKBOOK)

    def log_message(self, *args):
        pass


class EnrCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.sources = sources

    def test_fetch_file_posts_a_form_with_the_cookies_its_page_set(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        Handler.seen = []
        url = f"http://127.0.0.1:{server.server_port}/page.aspx"
        source = Source("loopback", "SD", "2026-11-03", url)
        settings = CollectorSettings()
        jar = CookieJar()
        fetch_file(source, settings, self.root / "page", {}, cookies=jar)
        result = fetch_file(
            source, settings, self.root / "export", {}, form=b"__VIEWSTATE=x", cookies=jar
        )
        self.assertEqual(result.headers["content-type"], enr_export.WORKBOOK)
        self.assertEqual(Handler.seen[0][:3], ("GET", "/page.aspx", None))
        self.assertEqual(Handler.seen[1], ("POST", "/page.aspx", "session=abc", b"__VIEWSTATE=x"))
        # Without a jar the shared client still sends no cookies (unchanged default).
        fetch_file(source, settings, self.root / "again", {})
        self.assertIsNone(Handler.seen[2][2])

    def serve(self, code, calls, workbook=enr_export.WORKBOOK):
        source = self.sources[code.lower() + "_2026_general"]
        data = members(code)
        urls = {url: name for name, url in (*source.files, (source.primary_filename, source.url))}

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            self.assertIsInstance(cookies, CookieJar)
            calls.append(("GET" if form is None else "POST", url, form))
            if form is None:
                body, kind = data[urls[url]], "text/csv"
            else:
                fields = parse_qs(form.decode())
                self.assertIn("__VIEWSTATE", fields)
                self.assertIn("__EVENTVALIDATION", fields)
                if code == "SD":
                    body = data["statewide.xlsx"]
                else:
                    page = data["sw.html" if "type=SW" in url else "fed.html"].decode(
                        "utf-8", errors="replace"
                    )
                    race = re.search(
                        rf'name="{re.escape(fields["__EVENTTARGET"][0])}"[^>]*raceid="(\d+)"', page
                    )
                    member = {
                        "11185": "county-nm-sen.xlsx",
                        "11186": "county-nm-01.xlsx",
                        "11187": "county-nm-02.xlsx",
                        "11188": "county-nm-03.xlsx",
                        "11530": "county-nm-gov.xlsx",
                    }[race[1]]
                    body = data[member]
                kind = workbook
            path.write_bytes(body)
            return FetchResponse(
                200, {"content-type": kind}, url, len(body), hashlib.sha256(body).hexdigest()
            )

        return source, fetch

    def test_bundle_posts_each_official_export_and_matches_the_fixture(self):
        for code in ("SD", "NM"):
            with self.subTest(code=code):
                calls = []
                source, fetch = self.serve(code, calls)
                destination = self.root / f"{code}.zip"
                with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
                    result = bundles.fetch_bundle(source, self.settings, destination)
                with zipfile.ZipFile(destination) as archive:
                    self.assertEqual(
                        {n: archive.read(n) for n in archive.namelist()}, members(code)
                    )
                posts = [c for c in calls if c[0] == "POST"]
                self.assertEqual(len(posts), len(source.supplemental_members))
                for _, url, _ in posts:
                    self.assertEqual(url.split("/")[2], source.url.split("/")[2])
                self.assertEqual(
                    {c["name"] for c in result.components}, set(source.expected_members)
                )

    def test_unchanged_csv_reuses_a_fresh_bundle_but_refreshes_old_forced_or_changed(self):
        calls = []
        source, fetch = self.serve("SD", calls)
        first = self.root / "first.zip"
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            original = bundles.fetch_bundle(source, self.settings, first)
            calls.clear()
            again = bundles.fetch_bundle(source, self.settings, self.root / "b.zip", previous=first)
            self.assertEqual(again.sha256, original.sha256)
            self.assertEqual([c[0] for c in calls], ["GET"])
            calls.clear()
            bundles.fetch_bundle(
                source, self.settings, self.root / "c.zip", previous=first, force_download=True
            )
            self.assertIn("POST", [c[0] for c in calls])
            old = time.time() - enr_export.REFRESH_SECONDS - 5
            os.utime(first, (old, old))
            calls.clear()
            bundles.fetch_bundle(source, self.settings, self.root / "d.zip", previous=first)
            self.assertIn("POST", [c[0] for c in calls])

    def test_non_workbook_export_and_foreign_form_action_fail_closed(self):
        calls = []
        source, fetch = self.serve("SD", calls, workbook="text/html")
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            with self.assertRaises(FetchFailure):
                bundles.fetch_bundle(source, self.settings, self.root / "x.zip")
        page = (
            '<form action="https://evil.example/x">'
            '<input type="hidden" name="__VIEWSTATE" value="a">'
            '<input type="hidden" name="__EVENTVALIDATION" value="b"></form>'
        )
        with self.assertRaises(ValueError):
            enr_export.postback(
                page,
                "https://electionresults.sd.gov/ResultsExport.aspx",
                "ctl00$MainContent$Statewide",
            )
        with self.assertRaises(ValueError):
            enr_export.postback(
                page.replace("https://evil.example/x", ""),
                "https://electionresults.sd.gov/ResultsExport.aspx",
                "ctl00$MainContent$x');alert(1)//",
            )


@requires_database
class NewStateDatabaseTests(unittest.TestCase):
    def test_three_states_load_validate_publish_and_enforce_reader_permissions(self):
        db = migrated()
        for code in CODES:
            f = NewStateTests()
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
            with connect(db) as conn:
                conn.execute("SET ROLE election_dashboard")
                # The dashboard reads published results, never parsed rows.
                self.assertTrue(
                    conn.execute(
                        "SELECT has_table_privilege(current_user, 'serving.contest_results',"
                        " 'SELECT') AS ok"
                    ).fetchone()["ok"]
                )
                self.assertFalse(
                    conn.execute(
                        "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                    ).fetchone()["ok"]
                )
                conn.execute("RESET ROLE")
                county = conn.execute(
                    "SELECT count(*) AS n FROM serving.county_results "
                    "WHERE publication_id=(SELECT publication_id FROM serving.current_publications "
                    "WHERE source_id=%s)",
                    (f.source.source_id,),
                ).fetchone()["n"]
                self.assertGreater(county, 0)


if __name__ == "__main__":
    unittest.main()
