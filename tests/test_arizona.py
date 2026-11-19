"""Arizona's Enhanced Voting results export, saved and imported by hand.

The fixture is the export of November 18, 2026 (created 15:42:07 UTC) with every ballot
option's precinct detail removed, which the parser does not read
(``tests/fixtures/arizona/provenance.json``).
"""

import csv
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from election_pipeline.manual import import_file
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/arizona/export_without_precincts.json.gz"
DOWNLOADED = "2026-11-19T05:00:00+00:00"
METHODS = ("early_ballot", "early_ars_16_579", "late_early", "election_day", "provisional")
EXPECTED = {
    "AZ-GOV": {
        "Katie Hobbs / John Giles": 1434824,
        "Andy Biggs / Sine Kerr": 1094393,
        "Risa Lombardo / Lisa Castillo": 24644,
        "Teri Ann Hourihan / Angela Harrolle Mohan": 23520,
        "Alice Novoa": 18,
        "Gertritrude Gonzalez": 11,
        "Write-in": 1737,
    },
    "AZ-06": {
        "JoAnna Mendoza": 190058,
        "Juan Ciscomani": 160972,
        "Jereme Lance Peters": 4514,
        "Gary Swing": 3264,
        "Michael Dorland": 90,
        "Write-in": 326,
    },
}


class ArizonaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["az_2026_general"]
        self.raw = gzip.decompress(FIXTURE.read_bytes())

    def imported(self, raw):
        path = self.root / "inbox" / "export.json"
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

    def test_the_export_parses_to_reconciled_county_results(self):
        receipt, report = self.imported(self.raw)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-18T15:42:07.717016+00:00")
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), {"AZ-GOV", *(f"AZ-{n:02}" for n in range(1, 10))})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        counties = {r["county"] for r in self.read(report, "county_results.csv")}
        self.assertEqual(len(counties), 15)
        self.assertIn("Santa Cruz", counties)

    def test_each_count_is_stored_by_arizonas_five_vote_groups(self):
        _, report = self.imported(self.raw)
        rows = self.read(report, "results.csv")
        hobbs = next(
            r for r in rows if r["choice"] == "Katie Hobbs / John Giles" and not r["county"]
        )
        self.assertEqual(
            {m: int(hobbs[m]) for m in METHODS},
            {
                "early_ballot": 1209806,
                "early_ars_16_579": 23436,
                "late_early": 17901,
                "election_day": 180201,
                "provisional": 3480,
            },
        )
        for found in rows:
            self.assertEqual(sum(int(found[m]) for m in METHODS), int(found["total_votes"]))

    def test_vote_groups_that_do_not_add_up_are_refused(self):
        for change in ("count", "county", "name"):
            data = json.loads(self.raw)
            items = data["localResults"][0] if change == "county" else data["results"]
            governor = next(
                b for b in items["ballotItems"] if b["name"] == "Governor and Lieutenant Governor"
            )
            group = governor["ballotOptions"][0]["groupResults"][0]
            if change == "name":
                group["groupName"] = "Early Mail"
            else:
                # Moved between groups: the candidate's count still adds up, the county's
                # group no longer matches statewide (or the statewide group its counties).
                group["voteCount"] += 1
                governor["ballotOptions"][0]["groupResults"][1]["voteCount"] -= 1
            with self.subTest(change), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(json.dumps(data).encode())

    def test_a_county_that_does_not_add_up_is_refused(self):
        data = json.loads(self.raw)
        county = data["localResults"][0]
        governor = next(
            b for b in county["ballotItems"] if b["name"] == "Governor and Lieutenant Governor"
        )
        governor["ballotOptions"][0]["voteCount"] += 1
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(json.dumps(data).encode())

    def test_another_election_is_refused(self):
        data = json.loads(self.raw)
        data["electionDate"] = "2024-11-05"
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(json.dumps(data).encode())


if __name__ == "__main__":
    unittest.main()
