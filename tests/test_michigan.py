"""Michigan's county results export, imported by hand as a manual source.

The fixture is the Secretary of State's export as the operator downloaded it on
November 18, 2026, when the site showed it unofficial and updated 7:17:44 PM Eastern
(``tests/fixtures/michigan/provenance.json``). The export prints no report time, so the
operator declares the site's.
"""

import csv
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from election_pipeline.dashboard.server import STATIC
from election_pipeline.manual import import_file
from election_pipeline.parsers import mi
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/michigan"
DOWNLOADED = "2026-11-19T00:30:00+00:00"
UPDATED = "2026-11-18T19:17:44-05:00"
# The export after the Board of State Canvassers certified (the site's "Updated:" time).
CANVASSED = "2026-11-23T11:15:22-05:00"
CANVASSED_AT = "2026-11-25T17:00:00+00:00"
EXPECTED = {
    "MI-SEN": {
        "Abdul El-Sayed": 2521693,
        "Mike Rogers": 2180388,
        "Lydia Christensen": 49692,
        "Tim Long": 26534,
        "Douglas P. Marsh": 12553,
        "Walter P. Kristy": 7253,
        "Write-in": 10,
    },
    "MI-GOV": {
        "Jocelyn Benson": 2615034,
        "John James": 2094508,
        "Anthony Hudson": 44496,
        "Donna Brandenburg": 20136,
        "Douglas Campbell": 17624,
        "Write-in": 44,
    },
    "MI-07": {"William Lawrence": 202800, "Tom Barrett": 189500, "Shane Dedrick": 4973},
}


class MichiganTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["mi_2026_general"]
        self.raw = gzip.decompress((FIXTURE / "county_results.txt.gz").read_bytes())

    def imported(self, raw, declared=UPDATED, downloaded=DOWNLOADED):
        path = self.root / "inbox" / "2026STATE_GENERAL_MI_CENR_BY_COUNTY.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source,
            self.settings,
            self.root,
            path,
            downloaded,
            hashlib.sha256(raw).hexdigest(),
            declared_generated_at=declared,
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def read(self, report, filename):
        with (Path(report["output_dir"]) / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_downloaded_export(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), ledger["sha256"])
        self.assertEqual(len(self.raw), ledger["bytes"])

    def test_the_export_parses_to_county_sums_with_a_declared_report_time(self):
        receipt, report = self.imported(self.raw)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], UPDATED)
        self.assertEqual(receipt["source_generated_basis"], "operator_declared")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), set(state_adapter("MI").races))
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        self.assertEqual(report["write_in_votes"]["MI-SEN"], 10)
        self.assertEqual(report["total_voter_turnout"], 105403797)
        counties = self.read(report, "county_results.csv")
        self.assertEqual(
            {r["county"] for r in counties if r["contest_id"] == "MI-SEN"}, set(mi.COUNTIES)
        )
        alcona = {
            r["choice"]: int(r["total_votes"])
            for r in counties
            if r["county"] == "Alcona" and r["contest_id"] == "MI-GOV"
        }
        self.assertEqual(alcona["Jocelyn Benson"], 2000)
        self.assertEqual(alcona["John James"], 4141)
        # Every county is drawn on the map under the same key.
        geo = json.loads((STATIC / "mi-map.json").read_text())
        keys = {f["id"] for f in geo["counties"]}
        self.assertEqual({mi.re.sub(r"[^A-Z0-9]", "", c.upper()) for c in mi.COUNTIES}, keys)
        self.assertEqual(len(geo["districts"]), 13)

    def test_the_canvassed_export_is_labeled_official_by_its_last_line(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())["official_export"]
        official = gzip.decompress((FIXTURE / ledger["fixture"]).read_bytes())
        self.assertEqual(hashlib.sha256(official).hexdigest(), ledger["sha256"])
        self.assertEqual(len(official), ledger["bytes"])
        self.assertTrue(official.endswith(b"\tRESULTS: OFFICIAL\r\n"))
        # The election-week export carries no label and stays unofficial.
        self.assertIs(self.imported(self.raw)[1]["official_results"], False)
        receipt, report = self.imported(official, declared=CANVASSED, downloaded=CANVASSED_AT)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertIs(report["official_results"], True)
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        # The canvass moved the Senate, Governor and 12th District by a few hundred votes.
        self.assertEqual(statewide["MI-SEN"]["Abdul El-Sayed"], 2521908)
        self.assertEqual(statewide["MI-SEN"]["Mike Rogers"], 2180444)
        self.assertEqual(statewide["MI-GOV"]["Jocelyn Benson"], 2615251)
        self.assertEqual(statewide["MI-GOV"]["John James"], 2094566)
        self.assertEqual(statewide["MI-12"]["Rashida Tlaib"], 247682)
        self.assertEqual(statewide["MI-07"], EXPECTED["MI-07"])
        # Any other label is a wording not yet checked: refused.
        other = official.replace(b"RESULTS: OFFICIAL", b"RESULTS: CERTIFIED")
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(other, declared=CANVASSED, downloaded=CANVASSED_AT)

    def test_the_report_time_must_be_declared_and_only_where_the_file_has_none(self):
        with self.assertRaisesRegex(ValueError, "ISO report generation timestamp"):
            self.imported(self.raw, declared=None)
        with self.assertRaisesRegex(ValueError, "follows download"):
            self.imported(self.raw, declared="2026-11-19T01:00:00+00:00")

    def test_a_changed_export_is_refused(self):
        lines = self.raw.decode("utf-8").split("\r\n")
        benson = next(i for i, line in enumerate(lines) if "\tBenson\tJocelyn\t" in line)

        def edited(index, old, new):
            changed = list(lines)
            self.assertIn(old, changed[index])
            changed[index] = changed[index].replace(old, new, 1)
            return "\r\n".join(changed).encode()

        changes = {
            "party": edited(benson, "\tDEMOCRATIC\t", "\tGREEN\t"),
            "election": edited(benson, "2026-11-03", "2024-11-05"),
            "county": edited(benson, "\tALCONA\t", "\tALGER\t"),
            "count": edited(len(lines) - 2, "RECORDS: 8742", "RECORDS: 8741")
            if "RECORDS" in lines[-2]
            else edited(len(lines) - 1, "RECORDS: 8742", "RECORDS: 8741"),
            "votes": edited(benson, "\t2000\t", "\t2x00\t"),
        }
        for name, raw in changes.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(raw)


if __name__ == "__main__":
    unittest.main()
