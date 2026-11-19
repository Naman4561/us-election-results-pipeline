"""Montana's Statewide Export, as the collector captured it, and its collector.

The fixture is one bundle (export page and Statewide Export workbook) captured from the
results site on November 19, 2026 (``tests/fixtures/montana/provenance.json``).
"""

import csv
import hashlib
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
from urllib.parse import parse_qs

from test_new_states import locate, relabel, set_cells

from election_pipeline import bundles
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors import montana
from election_pipeline.dashboard.server import STATIC
from election_pipeline.parsers import mt
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/montana"
SENATE = "04069UNITED STATES SENATOR"
EXPECTED = {
    "MT-SEN": {
        "KURT ALME": 235693,
        "SETH BODNAR": 184574,
        "ALANI BANKHEAD": 59984,
        "KYLE AUSTIN": 9018,
    },
    "MT-01": {"SAM FORSTAG": 135132, "AARON FLINT": 124854, "NICK SHEEDY": 7721},
}


def members():
    with zipfile.ZipFile(FIXTURE / "bundle.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def redownloaded(raw, when=b"11/19/2026 12:05:00 PM"):
    """The same workbook as the site serves it again: a new download time only."""
    return relabel(raw, b"Downloaded at 11/19/2026 11:51:28 AM", b"Downloaded at " + when)


class MontanaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["mt_2026_general"]

    def parse(self, mutate=None):
        data = members()
        if mutate:
            mutate(data)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.getvalue()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.getvalue())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_captured_bundle(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(
            hashlib.sha256((FIXTURE / "bundle.zip").read_bytes()).hexdigest(), ledger["sha256"]
        )

    def test_the_export_parses_to_counties_that_add_up_to_its_totals(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["source_update_text"], "11/19/2026 11:50:20 AM MT")
        self.assertIs(report["official_results"], False)
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), {"MT-SEN", "MT-01", "MT-02"})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        counties = self.read(report, "county_results.csv")
        self.assertEqual(
            {r["county"] for r in counties if r["contest_id"] == "MT-SEN"}, set(mt.COUNTIES)
        )
        rows = self.read(report, "results.csv")
        senate = [r for r in rows if r["contest_id"] == "MT-SEN" and not r["county"]]
        self.assertEqual(
            {(r["precincts_reporting"], r["precincts_total"]) for r in senate}, {("726", "728")}
        )
        self.assertEqual({r["choice_party"] for r in senate}, {"REP", "LIB", "DEM", "IND"})
        # Every county is drawn on the map under the same key.
        geo = json.loads((STATIC / "mt-map.json").read_text())
        self.assertEqual(
            {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in mt.COUNTIES},
            {f["id"] for f in geo["counties"]},
        )
        self.assertEqual(len(geo["districts"]), 2)

    def test_changed_counts_totals_title_or_page_are_refused(self):
        book = members()["statewide.xlsx"]
        number, columns, _ = locate(book, SENATE, "Beaverhead")
        totals, _, _ = locate(book, SENATE, "TOTALS")
        alme = columns["KURT ALME Republican"]

        page = members()["export.html"]
        books = {
            "county": set_cells(book, SENATE, {f"{alme}{number}": "2806"}),
            "totals": set_cells(book, SENATE, {f"{alme}{totals}": "235694"}),
            "election": relabel(book, b"2026 Unofficial General", b"2024 Unofficial General"),
            # The export breaks a heading's lines with CRLF.
            "party": relabel(book, b"KURT ALME\r\nRepublican", b"KURT ALME\r\nWhig"),
        }
        changes = {name: {"statewide.xlsx": changed} for name, changed in books.items()} | {
            "page": {"export.html": page.replace(b"November 3, 2026", b"November 5, 2024")}
        }
        for name, change in changes.items():
            with self.subTest(name):
                report = self.parse(lambda m, change=change: m.update(change))
                self.assertEqual(report["outcome"], "validation_failed", report)

    def test_an_official_title_is_recorded_official(self):
        def certified(m):
            m["statewide.xlsx"] = relabel(
                m["statewide.xlsx"], b"2026 Unofficial General", b"2026 Official General"
            )

        self.assertIs(self.parse(certified)["official_results"], True)


class MontanaCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["mt_2026_general"]
        self.workbook = members()["statewide.xlsx"]

    def fetch(self, calls, kind=montana.WORKBOOK):
        page = members()["export.html"]

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            calls.append(("GET" if form is None else "POST", url))
            if form is None:
                body, content = page, "text/html"
            else:
                fields = parse_qs(form.decode())
                self.assertEqual(fields["__EVENTTARGET"], ["ctl00$MainContent$Statewide"])
                self.assertIn("__VIEWSTATE", fields)
                body, content = self.workbook, kind
            path.write_bytes(body)
            return FetchResponse(
                200, {"content-type": content}, url, len(body), hashlib.sha256(body).hexdigest()
            )

        return fetch

    def collect(self, name, calls, previous=None, **options):
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=self.fetch(calls)):
            return bundles.fetch_bundle(
                self.source, self.settings, self.root / name, previous=previous, **options
            )

    def test_the_export_button_is_posted_to_its_own_page(self):
        calls = []
        result = self.collect("first.zip", calls)
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertEqual({c[1] for c in calls}, {self.source.url})
        with zipfile.ZipFile(self.root / "first.zip") as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, members())
        self.assertEqual({c["name"] for c in result.components}, set(self.source.expected_members))

    def test_a_new_download_time_alone_reuses_the_bundle_but_new_counts_do_not(self):
        calls = []
        first = self.collect("first.zip", calls)
        self.workbook = redownloaded(self.workbook)
        self.assertNotEqual(self.workbook, members()["statewide.xlsx"])
        again = self.collect("again.zip", calls, previous=self.root / "first.zip")
        self.assertEqual(again.sha256, first.sha256)
        forced = self.collect(
            "forced.zip", calls, previous=self.root / "first.zip", force_download=True
        )
        self.assertNotEqual(forced.sha256, first.sha256)
        old = time.time() - montana.REFRESH_SECONDS - 5
        os.utime(self.root / "first.zip", (old, old))
        stale = self.collect("stale.zip", calls, previous=self.root / "first.zip")
        self.assertNotEqual(stale.sha256, first.sha256)
        os.utime(self.root / "first.zip")
        number, columns, _ = locate(self.workbook, SENATE, "Beaverhead")
        cell = columns["KURT ALME Republican"] + str(number)
        self.workbook = set_cells(self.workbook, SENATE, {cell: "2806"})
        changed = self.collect("changed.zip", calls, previous=self.root / "first.zip")
        self.assertNotEqual(changed.sha256, first.sha256)

    def test_anything_but_the_workbook_fails_closed(self):
        calls = []
        with patch(
            "election_pipeline.collectors.common.fetch_file",
            side_effect=self.fetch(calls, kind="text/html"),
        ):
            with self.assertRaises(FetchFailure):
                bundles.fetch_bundle(self.source, self.settings, self.root / "x.zip")
        self.workbook = b"PK not a workbook"
        with self.assertRaises(FetchFailure):
            self.collect("y.zip", calls)


if __name__ == "__main__":
    unittest.main()
