"""Missouri's All Results page, saved as a PDF by hand and imported as a manual source.

The fixture is the page as the operator saved it on November 18, 2026 ("Unofficial
Results as of 11/18/2026 10:53:42 PM"). It gives statewide totals for the eight U.S.
House districts, with no county breakdown.
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
FIXTURE = ROOT / "tests/fixtures/missouri/all_results.pdf"
DOWNLOADED = "2026-11-19T05:00:00+00:00"
EXPECTED = {
    "MO-01": {"Wesley Bell": 189175, "Paul Berry III": 32308, "Tom Schmitz": 10613},
    "MO-02": {"Fred Wellman": 169477, "Ann Wagner": 153132, "Brandon Coulter Daugherty": 8782},
    "MO-04": {
        "Mark Alford": 154236,
        "Jordan Herrera": 89182,
        "Thomas Holbrook": 7289,
        "Write-in": 241,
    },
}


class MissouriTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["mo_2026_general"]

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
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def test_the_page_parses_to_district_totals_with_its_report_time(self):
        receipt, report = self.imported(FIXTURE.read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-18T22:53:42-06:00")
        self.assertEqual(receipt["source_generated_basis"], "document")
        self.assertIs(report["official_results"], False)
        with (Path(report["output_dir"]) / "contest_results.csv").open(encoding="utf-8") as f:
            statewide = {}
            for found in csv.DictReader(f):
                statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                    found["total_votes"]
                )
        self.assertEqual(set(statewide), {f"MO-{n:02}" for n in range(1, 9)})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        self.assertNotIn("Write-in", statewide["MO-01"])  # No write-in line printed.

    def test_a_page_missing_a_district_is_refused(self):
        reader, writer = PdfReader(FIXTURE), PdfWriter()
        writer.add_page(reader.pages[0])  # Districts 6-8 are on page 2.
        path = self.root / "short.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
