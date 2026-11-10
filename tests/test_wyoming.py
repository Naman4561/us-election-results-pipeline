"""Wyoming's certified results, imported as a manual source's certified file.

The fixture is the Secretary of State's official results zip as posted on November 10,
2026 (``tests/fixtures/wyoming/provenance.json``).
"""

import csv
import hashlib
import io
import re
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline.manual import import_file
from election_pipeline.parsers import wy
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/wyoming/2026_Wyoming_General_Results.zip"
SHEET = "xl/worksheets/sheet2.xml"  # "Statewide Candidates"
EXPECTED = {
    "WY-SEN": {"Harriet Hageman": 122174, "James Byrd": 65105, "Write-in": 1039},
    "WY-AL": {
        "Chuck Gray": 99906,
        "Lisa Kinney": 61689,
        "Shawn Johnson": 13667,
        "Jeffrey Haggit": 7961,
        "Write-in": 1031,
    },
    "WY-GOV": {
        "Eric Barlow": 128496,
        "Kenneth R. Casner": 27472,
        "Rebecca Bextel": 31444,
        "Write-in": 1212,
    },
}


class WyomingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["wy_2026_general"]

    def imported(self, raw):
        path = self.root / "inbox" / "2026_Wyoming_General_Results.zip"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source,
            self.settings,
            self.root,
            path,
            datetime.now(timezone.utc).isoformat(),
            hashlib.sha256(raw).hexdigest(),
            certified=wy.RESULTS,
        )
        return parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])

    def read(self, report, filename):
        with (Path(report["output_dir"]) / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def changed(self, change):
        """The official zip with its Statewide Candidates sheet changed."""
        with zipfile.ZipFile(FIXTURE) as outer:
            members = {n: outer.read(n) for n in outer.namelist()}
        with zipfile.ZipFile(io.BytesIO(members[wy.SUMMARIES])) as inner:
            parts = {n: inner.read(n) for n in inner.namelist()}
        parts[SHEET] = change(parts[SHEET].decode()).encode()
        book = io.BytesIO()
        with zipfile.ZipFile(book, "w", zipfile.ZIP_DEFLATED) as inner:
            for name, value in parts.items():
                inner.writestr(name, value)
        members[wy.SUMMARIES] = book.getvalue()
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as outer:
            for name, value in members.items():
                outer.writestr(name, value)
        return result.getvalue()

    def test_the_certified_results_parse_to_the_official_totals(self):
        report = self.imported(FIXTURE.read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["certified_format"], "wy_results_workbook")
        self.assertEqual(report["contest_total_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, EXPECTED)
        counties = self.read(report, "county_results.csv")
        self.assertEqual(len({r["county"] for r in counties}), 23)
        self.assertEqual(len(counties), 23 * sum(len(v) for v in EXPECTED.values()))
        teton = {
            r["choice"]: int(r["total_votes"])
            for r in counties
            if r["county"] == "Teton" and r["contest_id"] == "WY-SEN"
        }
        self.assertEqual(teton, {"Harriet Hageman": 2744, "James Byrd": 7557, "Write-in": 71})

    def test_a_county_that_does_not_add_up_or_an_unknown_party_is_refused(self):
        def off_by_one(sheet):
            return re.sub(r'(<c r="B5"[^>]*><v>)5450(</v>)', r"\g<1>5451\g<2>", sheet)

        def total_changed(sheet):
            return re.sub(r'(<c r="N28"[^>]*><v>)128496(</v>)', r"\g<1>128497\g<2>", sheet)

        for change in (off_by_one, total_changed):
            raw = self.changed(change)
            self.assertNotEqual(raw, FIXTURE.read_bytes())
            with self.subTest(change.__name__), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(raw)

    def test_wyoming_has_no_feed_to_parse(self):
        with self.assertRaisesRegex(ValueError, "certified results only"):
            wy.parse(FIXTURE, self.source, self.settings, self.root, {})


if __name__ == "__main__":
    unittest.main()
