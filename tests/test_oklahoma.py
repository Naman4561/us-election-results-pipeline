"""Oklahoma's county results export, imported by hand as a manual source.

The fixture is the State Election Board's export as downloaded on November 11, 2026
(``tests/fixtures/oklahoma/provenance.json``).
"""

import csv
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from election_pipeline.manual import import_file
from election_pipeline.parsers import ok
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/oklahoma/20261103_CountyResults_csv.zip"
DOWNLOADED = "2026-11-11T01:58:18+00:00"
EXPECTED = {
    "OK-SEN": {
        "KEVIN HERN": 639860,
        "N'KIYLA JASMINE THOMAS": 439522,
        "SEVIER WHITE": 10209,
        "RON MEINHARDT": 14504,
        "CURTIS STINNETT": 14635,
    },
    "OK-GOV": {
        "MIKE MAZZEI": 582039,
        "CYNDI MUNSON": 514776,
        "ORLANDO LYNN BUSH": 5014,
        "ROBERT E BROOKS SR": 11660,
        "JERRY GRIFFIN": 7441,
    },
    "OK-01": {"MARK TEDFORD": 121397, "JOHN CROISANT": 110707},
    "OK-02": {"JOSH BRECHEEN": 144054, "BRANDON WADE": 61409, "RONNIE HOPKINS": 6520},
    "OK-03": {"FRANK D. LUCAS": 122181, "SUZIE BYRD": 68832},
    "OK-04": {"TOM COLE": 125631, "MITCHELL JACOB": 86108, "ROCCO BONACCI": 6389},
    "OK-05": {
        "STEPHANIE BICE": 131023,
        "JENA NELSON": 127687,
        "ROBERT P. HENRI": 4027,
        "AUSTIN NIEVES": 2047,
    },
}


class OklahomaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ok_2026_general"]

    def imported(self, raw, certified=None):
        path = self.root / "inbox" / FIXTURE.name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source,
            self.settings,
            self.root,
            path,
            DOWNLOADED,
            hashlib.sha256(raw).hexdigest(),
            certified=certified,
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def read(self, report, filename):
        with (Path(report["output_dir"]) / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def changed(self, change):
        """The export with its CSV changed, its member keeping the original date."""
        with zipfile.ZipFile(FIXTURE) as export:
            info = export.getinfo(ok.MEMBER)
            text = export.read(info).decode("utf-8-sig")
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as export:
            export.writestr(zipfile.ZipInfo(ok.MEMBER, info.date_time), change(text))
        return result.getvalue()

    def test_the_export_parses_to_county_sums_with_vote_methods(self):
        receipt, report = self.imported(FIXTURE.read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-11T01:58:16+00:00")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, EXPECTED)
        counties = self.read(report, "county_results.csv")
        self.assertEqual(
            {r["county"] for r in counties if r["contest_id"] == "OK-SEN"}, set(ok.COUNTIES)
        )
        adair = {
            r["choice"]: (
                int(r["absentee_by_mail"]),
                int(r["early_voting"]),
                int(r["election_day"]),
                int(r["total_votes"]),
            )
            for r in counties
            if r["county"] == "Adair" and r["contest_id"] == "OK-GOV"
        }
        self.assertEqual(adair["MIKE MAZZEI"], (39, 302, 2839, 3180))
        self.assertIn("Le Flore", {r["county"] for r in counties})

    def test_methods_that_do_not_add_up_or_an_unknown_party_are_refused(self):
        changes = {
            "methods": lambda text: text.replace(
                '"MIKE MAZZEI",REP,39,302,2839,3180', '"MIKE MAZZEI",REP,39,302,2839,3181', 1
            ),
            "party": lambda text: text.replace('"MIKE MAZZEI",REP', '"MIKE MAZZEI",GRN', 1),
            "election": lambda text: text.replace("11/3/2026", "11/5/2024", 1),
        }
        for name, change in changes.items():
            raw = self.changed(change)
            self.assertNotEqual(raw, self.changed(lambda text: text))
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(raw)

    def test_the_official_export_is_imported_as_the_certified_file_and_checked_alike(self):
        name = "certified-county-results.zip"
        receipt, report = self.imported(FIXTURE.read_bytes(), certified=name)
        self.assertEqual(receipt["certified_file"], name)
        archive = ArchiveStore(self.root, self.source).archive_path(receipt["sha256"])
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), [name])
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["source_generated_at"], "2026-11-11T01:58:16+00:00")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, EXPECTED)
        # The certified file gets the export's checks: a changed one is refused too.
        raw = self.changed(lambda text: text.replace('"MIKE MAZZEI",REP', '"MIKE MAZZEI",GRN', 1))
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(raw, certified=name)


if __name__ == "__main__":
    unittest.main()
