"""Alabama's statewide results page and its Export Data workbook, and their collector.

The fixture is one bundle captured on November 19, 2026
(``tests/fixtures/alabama/provenance.json``).
"""

import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from test_new_states import relabel

from election_pipeline import bundles
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors import alabama
from election_pipeline.dashboard.server import STATIC
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/alabama"
EXPECTED = {
    "AL-GOV": {"Tommy Tuberville": 872561, "Doug Jones": 806253, "Write-in": 5921},
    "AL-SEN": {"Barry Moore": 917860, "Everett Wess": 758465, "Write-in": 2047},
    "AL-02": {"Shomari C. Figures": 124422, "Rhett Marques": 107049, "Write-in": 338},
}


def members():
    with zipfile.ZipFile(FIXTURE / "bundle.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def rewritten(raw):
    """The same workbook written again as a new download (new zip timestamps)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(out, "w") as new:
        for item in old.infolist():
            new.writestr(zipfile.ZipInfo(item.filename, (2026, 11, 19, 15, 0, 0)), old.read(item))
    return out.getvalue()


class AlabamaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["al_2026_general"]

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
        digest = hashlib.sha256((FIXTURE / "bundle.zip").read_bytes()).hexdigest()
        self.assertEqual(digest, ledger["sha256"])

    def test_counties_add_up_to_the_statewide_page(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["source_update_text"], "11/04/2026 11:16:06 AM CT")
        self.assertEqual(report["counties_reported"], "67 of 67")
        self.assertIs(report["official_results"], False)
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), {"AL-GOV", "AL-SEN", *(f"AL-0{n}" for n in range(1, 8))})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        counties = self.read(report, "county_results.csv")
        names = {r["county"] for r in counties if r["contest_id"] == "AL-GOV"}
        self.assertEqual(len(names), 67)
        jefferson = [r for r in self.read(report, "results.csv") if r["county"] == "Jefferson"]
        self.assertEqual(
            {(r["precincts_reporting"], r["precincts_total"]) for r in jefferson}, {("178", "178")}
        )
        geo = json.loads((STATIC / "al-map.json").read_text())
        self.assertEqual(
            {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in names}, {f["id"] for f in geo["counties"]}
        )
        self.assertEqual(len(geo["districts"]), 7)

    def test_a_county_or_page_that_disagrees_is_refused(self):
        page = members()["results.html"]
        book = members()["export.xlsx"]
        changes = {
            # Tuberville's statewide figure one vote higher than his counties.
            "page votes": {"results.html": page.replace(b"872,561", b"872,562", 1)},
            "election": {"results.html": page.replace(b"2026 GENERAL", b"2024 GENERAL")},
            "county votes": {"export.xlsx": relabel(book, b"<v>168633</v>", b"<v>168634</v>")},
            "party": {"export.xlsx": relabel(book, b"Tommy Tuberville", b"Tommy Tuberville (W)")},
        }
        for name, change in changes.items():
            with self.subTest(name):
                report = self.parse(lambda m, change=change: m.update(change))
                self.assertEqual(report["outcome"], "validation_failed", report)


class AlabamaCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["al_2026_general"]
        self.data = members()

    def fetch(self, calls, kind=alabama.WORKBOOK):
        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            calls.append(("GET" if form is None else "POST", url))
            if form is None:
                body, content = self.data["results.html"], "text/html"
            else:
                fields = parse_qs(form.decode())
                self.assertEqual(fields["__EVENTTARGET"], ["hlnkExportData"])
                self.assertIn("__VIEWSTATE", fields)
                body, content = self.data["export.xlsx"], kind
            path.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            return FetchResponse(200, {"content-type": content}, url, len(body), digest)

        return fetch

    def collect(self, name, calls, previous=None, kind=alabama.WORKBOOK):
        with patch(
            "election_pipeline.collectors.common.fetch_file", side_effect=self.fetch(calls, kind)
        ):
            return bundles.fetch_bundle(
                self.source, self.settings, self.root / name, previous=previous
            )

    def test_the_results_form_is_posted_to_its_own_page(self):
        calls = []
        self.collect("first.zip", calls)
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertTrue(calls[1][1].startswith("https://www2.alabamavotes.gov/electionNight/"))
        with zipfile.ZipFile(self.root / "first.zip") as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, self.data)

    def test_a_rewritten_workbook_reuses_the_bundle_but_new_counts_do_not(self):
        calls = []
        first = self.collect("first.zip", calls)
        self.data["export.xlsx"] = rewritten(self.data["export.xlsx"])
        again = self.collect("again.zip", calls, previous=self.root / "first.zip")
        self.assertEqual(again.sha256, first.sha256)
        self.data["export.xlsx"] = relabel(
            self.data["export.xlsx"], b"<v>168633</v>", b"<v>168634</v>"
        )
        changed = self.collect("changed.zip", calls, previous=self.root / "first.zip")
        self.assertNotEqual(changed.sha256, first.sha256)

    def test_anything_but_the_workbook_or_a_page_without_its_form_fails(self):
        with self.assertRaises(FetchFailure):
            self.collect("x.zip", [], kind="text/html")
        self.data["results.html"] = self.data["results.html"].replace(
            b'name="form1"', b'name="other"'
        )
        with self.assertRaises(FetchFailure):
            self.collect("y.zip", [])


if __name__ == "__main__":
    unittest.main()
