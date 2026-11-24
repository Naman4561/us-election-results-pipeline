"""Maine's official tabulations and District 2 ranked-choice summary, imported by hand.

The fixture packs the files the operator saved on November 23, 2026, unchanged
(``tests/fixtures/maine/provenance.json``).
"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from test_new_states import relabel

from election_pipeline.dashboard.server import STATIC
from election_pipeline.manual import import_file
from election_pipeline.parsers import me
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/maine"
DOWNLOADED = "2026-11-23T20:48:35+00:00"
DECLARED = "2026-11-23T15:00:00-05:00"
EXPECTED = {
    "ME-SEN": {"Susan M. Collins": 338596, "Troy D. Jackson": 379629, "Write-in": 527},
    "ME-GOV": {
        "Rick Bennett": 58241,
        "Robert B. Charles": 272710,
        "Hannah M. Pingree": 391364,
        "Write-in": 109,
    },
    "ME-01": {"Chellie Pingree": 252194, "Ronald C. Russell": 130155},
    "ME-02": {"Matthew G. Dunlap": 164889, "Paul R. LePage": 170419, "Write-in": 125},
}
# Military and overseas ballots' candidate votes, in no county.
UOCAVA = {"ME-SEN": 4823, "ME-GOV": 4818, "ME-01": 2897, "ME-02": 1942}
SENATE = "US Senate - FINAL.xlsx"
DISTRICT_2 = "Rep to Congress District 2.xlsx"


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def packed(data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), value)
    return out.getvalue()


class MaineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["me_2026_general"]

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

    def test_the_fixture_packs_the_saved_files(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(
            hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest(), ledger["sha256"]
        )
        self.assertEqual(
            {n: hashlib.sha256(v).hexdigest() for n, v in members().items()}, ledger["members"]
        )

    def test_every_race_by_county_with_overseas_ballots_statewide_only(self):
        report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["statewide_only_votes"], UOCAVA)
        self.assertEqual(report["ballot_totals"]["ME-SEN"], 731486)
        self.assertEqual(report["ranked_choice"]["ME-02"]["first_round_votes"], 170710)
        self.assertEqual(
            report["places_without_county_code"], {"ME-SEN": ["Woodstock/Milton Twp (Oxford)"]}
        )
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, EXPECTED)
        counties = {}
        for found in self.read(report, "county_results.csv"):
            race = counties.setdefault(found["contest_id"], {})
            race[found["choice"]] = race.get(found["choice"], 0) + int(found["total_votes"])
        for race, votes in counties.items():
            self.assertEqual(sum(EXPECTED[race].values()) - sum(votes.values()), UOCAVA[race])
        geo = json.loads((STATIC / "me-map.json").read_text())
        self.assertEqual(
            {c.upper() for c in me.COUNTIES.values()}, {f["id"] for f in geo["counties"]}
        )
        self.assertEqual(len(geo["districts"]), 2)

    def test_a_changed_or_missing_file_is_refused(self):
        senate, district = members()[SENATE], members()[DISTRICT_2]

        def text(raw, old, new):
            # A text or number cell's content, whichever the sheet stores it as.
            return relabel(raw, f">{old}<".encode(), f">{new}<".encode())

        summary = members()[me.RCV_SUMMARY]
        changes = {
            # A town's count no longer adds up to its ballots cast.
            "count": {SENATE: text(senate, "4959", "4960")},
            "party": {SENATE: text(senate, "Republican", "Whig")},
            "county": {DISTRICT_2: text(district, "WAS Total", "XYZ Total")},
            "ranked choice": {me.RCV_SUMMARY: summary.replace(b"341526", b"341527")},
        }
        for name, change in changes.items():
            raw = next(iter(change.values()))
            self.assertNotEqual(raw, members()[next(iter(change))], name)
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(packed(members() | change))
        with self.assertRaisesRegex(ValueError, "rejected"):
            data = members()
            data.pop(DISTRICT_2)
            self.imported(packed(data))


if __name__ == "__main__":
    unittest.main()
