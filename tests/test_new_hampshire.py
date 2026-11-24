"""New Hampshire's four results spreadsheets, imported by hand as one zip.

The fixture packs the spreadsheets the operator downloaded on November 19, 2026,
unchanged (``tests/fixtures/new_hampshire/provenance.json``).
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

from test_new_states import relabel

from election_pipeline.dashboard.server import STATIC
from election_pipeline.manual import import_file
from election_pipeline.parsers import nh
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/new_hampshire"
DOWNLOADED = "2026-11-19T21:02:00+00:00"
DECLARED = "2026-11-19T16:01:00-05:00"
EXPECTED = {
    "NH-SEN": {
        "Chris Pappas": 356425,
        "John E. Sununu": 268682,
        "Edmond Laplante": 12081,
        "Write-in": 792,
    },
    "NH-GOV": {
        "Kelly Ayotte": 324734,
        "Cinde Warmington": 296834,
        "Stephen Villee": 9630,
        "Write-in": 745,
    },
    "NH-01": {"Stefany Shaheen": 180763, "Anthony DiLorenzo": 136115, "Write-in": 536},
    "NH-02": {
        "Maggie Goodlander": 185900,
        "Lily Tang Williams": 122645,
        "Robbie Mahrou": 7478,
        "Write-in": 234,
    },
}
SENATE = "2026-ge-us-senator-summary.xlsx"
DISTRICT = "2026-ge-congressional-district-1.xlsx"


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def packed(data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), value)
    return out.getvalue()


class NewHampshireTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["nh_2026_general"]

    def imported(self, raw):
        path = self.root / "inbox" / "results.zip"
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
        return parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_packs_the_downloaded_spreadsheets(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(
            hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest(), ledger["sha256"]
        )
        self.assertEqual(
            {n: hashlib.sha256(v).hexdigest() for n, v in members().items()}, ledger["members"]
        )

    def test_every_race_by_county_with_house_towns_added_up(self):
        report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, EXPECTED)
        counties = {}
        for found in self.read(report, "county_results.csv"):
            counties.setdefault(found["contest_id"], set()).add(found["county"])
        self.assertEqual(counties["NH-SEN"], set(nh.COUNTIES))
        self.assertEqual(len(counties["NH-01"]) + len(counties["NH-02"]), 15)
        # Dover's six wards count for Strafford in District 1.
        strafford = {
            r["choice"]: int(r["total_votes"])
            for r in self.read(report, "county_results.csv")
            if r["contest_id"] == "NH-01" and r["county"] == "Strafford"
        }
        dover = 2070 + 1949 + 2089 + 1980 + 1589 + 1593
        self.assertGreater(strafford["Stefany Shaheen"], dover)
        geo = json.loads((STATIC / "nh-map.json").read_text())
        self.assertEqual({c.upper() for c in nh.COUNTIES}, {f["id"] for f in geo["counties"]})
        self.assertEqual(len(geo["districts"]), 2)

    def test_a_changed_sheet_or_an_unknown_town_is_refused(self):
        senate, district = members()[SENATE], members()[DISTRICT]

        def text(raw, old, new):
            # A text or number cell's content, whichever the sheet stores it as.
            return relabel(raw, f">{old}<".encode(), f">{new}<".encode())

        changes = {
            # A county's count no longer adds up to the TOTALS Excel stored.
            "count": {SENATE: text(senate, "14884", "14885")},
            "party": {SENATE: text(senate, "Chris Pappas, d", "Chris Pappas, x")},
            "election": {SENATE: text(senate, "November 03, 2026", "November 05, 2024")},
            "town": {DISTRICT: text(district, "Alton", "Altonia")},
        }
        for name, change in changes.items():
            raw = next(iter(change.values()))
            self.assertNotEqual(raw, members()[next(iter(change))], name)
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(packed(members() | change))
        with self.assertRaisesRegex(ValueError, "rejected"):
            data = members()
            data.pop("2026-ge-congressional-district-2.xlsx")
            self.imported(packed(data))

    def test_every_house_place_has_one_county(self):
        towns = nh.towns()
        self.assertEqual(len(towns), 259)
        self.assertEqual(towns["Hale's Location"], "Carroll")
        self.assertEqual(towns["Wentworth"], "Grafton")
        self.assertEqual(towns["Wentworth's Location"], "Coos")
        self.assertTrue(all(not re.search(r" Ward [0-9]+$", town) for town in towns))


if __name__ == "__main__":
    unittest.main()
