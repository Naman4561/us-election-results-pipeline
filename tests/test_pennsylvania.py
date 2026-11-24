"""Pennsylvania's county results export, imported by hand as a manual source.

The fixture is the Department of State's export as the operator downloaded it on
November 19, 2026, 12:32:52 PM Eastern (``tests/fixtures/pennsylvania/provenance.json``);
its Governor totals are the ones the results site showed. The export prints no report
time, so the operator declares the site's.
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
from election_pipeline.parsers import pa
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/pennsylvania"
DOWNLOADED = "2026-11-19T17:33:00+00:00"
UPDATED = "2026-11-19T12:32:52-05:00"
EXPECTED = {
    "PA-GOV": {"JOSH SHAPIRO": 3039717, "STACY GARRITY": 1914345, "KEN KRAWCHUK": 64992},
    "PA-03": {"CHRIS RABB": 258437, "DENNIS MAHONEY": 17628},
    "PA-15": {"GLENN GT THOMPSON": 171287, "RAY BILGER": 97263},
}


class PennsylvaniaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["pa_2026_general"]
        self.raw = gzip.decompress((FIXTURE / "county_results.txt.gz").read_bytes())

    def imported(self, raw, declared=UPDATED):
        path = self.root / "inbox" / "UnOfficial_11192026123252PM.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source,
            self.settings,
            self.root,
            path,
            DOWNLOADED,
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

    def test_the_export_parses_to_county_sums_by_vote_method(self):
        receipt, report = self.imported(self.raw)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_basis"], "operator_declared")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), set(state_adapter("PA").races))
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        counties = self.read(report, "county_results.csv")
        self.assertEqual(
            {r["county"] for r in counties if r["contest_id"] == "PA-GOV"}, set(pa.COUNTIES)
        )
        # Bedford's export row, method by method.
        rows = self.read(report, "results.csv")
        bedford = next(
            r for r in rows if r["county"] == "Bedford" and r["choice"] == "JOSH SHAPIRO"
        )
        self.assertEqual(
            [int(bedford[m]) for m in ("total_votes", "election_day", "mail", "provisional")],
            [4570, 3015, 1542, 13],
        )
        self.assertIn("McKean", {r["county"] for r in rows})
        # Every county is drawn on the map under the same key.
        geo = json.loads((STATIC / "pa-map.json").read_text())
        keys = {f["id"] for f in geo["counties"]}
        self.assertEqual({pa.re.sub(r"[^A-Z0-9]", "", c.upper()) for c in pa.COUNTIES}, keys)
        self.assertEqual(len(geo["districts"]), 17)

    def test_a_changed_export_is_refused(self):
        lines = self.raw.decode("utf-8").splitlines(keepends=True)
        shapiro = next(i for i, line in enumerate(lines) if '"ADAMS","Governor"' in line)

        def edited(old, new):
            changed = list(lines)
            self.assertIn(old, changed[shapiro])
            changed[shapiro] = changed[shapiro].replace(old, new, 1)
            return "".join(changed).encode()

        changes = {
            "party": edited('"Democratic"', '"Green"'),
            "election": edited("2026 General Election", "2024 General Election"),
            "county": edited('"ADAMS"', '"ALLEGHENY"'),
            "methods": edited('"7,244"', '"7,245"'),
            "votes": edited('"19,075"', '"19x075"'),
            "question": edited('"19,075","0"', '"19,075","1"'),
        }
        for name, raw in changes.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(raw)


if __name__ == "__main__":
    unittest.main()
