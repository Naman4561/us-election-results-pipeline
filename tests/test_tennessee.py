"""Tennessee's Followed Races dashboard, saved as a PDF by hand and imported as a manual
source. The fixture is the page as the operator saved it ("Final Unofficial Results, 95 of
95 Counties Completed"). It prints no report time, so the operator declares one.
"""

import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from election_pipeline.manual import import_file
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/tennessee/followed_races.pdf"
DOWNLOADED = "2026-11-19T05:00:00+00:00"
DECLARED = "2026-11-18T23:00:00-05:00"
EXPECTED = {
    "TN-SEN": {
        "Bill Hagerty": 1138436,
        "Marquita Bradshaw": 886974,
        "Robert Jones": 9398,
        "Tharon Chandler": 7689,
        "Andrew Gerena": 5633,
        'Catherine Barcel "Barcy" Whitson': 5113,
        "Jeremy Dean Hearn": 4937,
        "David Sutman, Jr.": 3087,
        "Yoshi D. Matthews": 3026,
        "James William Macon III": 2576,
    },
    "TN-04": {
        "Victoria Broderick": 95585,
        "Scott DesJarlais": 95305,
        "Jacob Kristopher Anders": 2833,
        "Clay Faircloth": 1853,
    },
}


class TennesseeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["tn_2026_general"]

    def imported(self, raw):
        path = self.root / "inbox" / "results.pdf"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source,
            self.settings,
            self.root,
            path,
            DOWNLOADED,
            hashlib.sha256(raw).hexdigest(),
            declared_generated_at=DECLARED,
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def test_the_page_parses_to_statewide_totals(self):
        receipt, report = self.imported(FIXTURE.read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_basis"], "operator_declared")
        self.assertEqual(report["percentage_reconciliation"], "passed")
        with (Path(report["output_dir"]) / "contest_results.csv").open(encoding="utf-8") as f:
            statewide = {}
            for found in csv.DictReader(f):
                statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                    found["total_votes"]
                )
        self.assertEqual(
            set(statewide), {"TN-GOV", "TN-SEN", *(f"TN-{n:02}" for n in range(1, 10))}
        )
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        self.assertEqual(statewide["TN-GOV"]["Marsha Blackburn"], 957136)
        self.assertEqual(len(statewide["TN-GOV"]), 15)

    def test_a_page_missing_races_is_refused(self):
        reader, writer = PdfReader(FIXTURE), PdfWriter()
        for page in reader.pages[:2]:
            writer.add_page(page)
        path = self.root / "short.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
