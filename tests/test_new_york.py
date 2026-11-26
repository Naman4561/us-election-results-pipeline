"""New York's State Board election night results page, saved as a PDF by hand.

The fixture is the page as the operator saved it (updated 11/18/2026 3:50:10 PM), with
Governor and the 26 congressional districts by county. Candidates run on several party
lines; each candidate's lines, each area's Total Votes and each contest's counties are
checked.
"""

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from election_pipeline.dashboard.server import STATIC, county_key
from election_pipeline.manual import import_file
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/new_york/election_night.pdf"
DOWNLOADED = "2026-11-19T05:15:00+00:00"


class NewYorkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["ny_2026_general"]

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

    def read(self, report, filename):
        with (Path(report["output_dir"]) / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_page_parses_with_fusion_lines_summed_and_counties_reconciled(self):
        receipt, report = self.imported(FIXTURE.read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-18T15:50:10-05:00")
        self.assertEqual(report["county_reconciliation"], "passed")
        self.assertEqual(report["party_line_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = (
                found["choice_party"],
                int(found["total_votes"]),
            )
        self.assertEqual(set(statewide), {"NY-GOV", *(f"NY-{n:02}" for n in range(1, 27))})
        governor = statewide["NY-GOV"]
        # DEM 3,260,046 + WOR 374,047; REP 2,148,988 + CON 235,280 + Vote Affordable 40,824.
        self.assertEqual(governor["Kathy C. Hochul / Adrienne E. Adams"], ("DEM", 3634093))
        self.assertEqual(governor["Bruce A. Blakeman / Todd Hood"], ("REP", 2425092))
        self.assertEqual(governor["Write-in"], ("", 12782))
        self.assertEqual(statewide["NY-14"]["Alexandria Ocasio-Cortez"][1], 107129)
        self.assertEqual(statewide["NY-15"]["Andre Easton"][0], "PSL")
        counties = {r["county"] for r in self.read(report, "county_results.csv")}
        geo = json.loads((STATIC / "ny-map.json").read_text())
        self.assertEqual({county_key(c) for c in counties}, {f["id"] for f in geo["counties"]})

    def test_a_page_missing_its_districts_is_refused(self):
        reader, writer = PdfReader(FIXTURE), PdfWriter()
        for page in reader.pages[:20]:
            writer.add_page(page)
        path = self.root / "short.pdf"
        with path.open("wb") as handle:
            writer.write(handle)
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
