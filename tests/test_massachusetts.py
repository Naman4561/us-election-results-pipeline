"""Massachusetts's PD43+ city and town results, as captured on November 23, 2026.

The fixture is the bundle of the eleven race CSVs (``tests/fixtures/massachusetts``,
whose ``provenance.json`` records each file's address, SHA-256 and capture time).
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

from election_pipeline.dashboard.server import STATIC
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/massachusetts"
EXPECTED = {
    "MA-SEN": {
        ("Edward J. Markey", "DEM"): 1718276,
        ("John Deaton", "REP"): 855969,
        ("Shiva Ayyadurai", "IND"): 61085,
        ("Joe Tache", "IND"): 59097,
        ("Write-in", ""): 3811,
    },
    "MA-GOV": {
        ("Healey and Driscoll", "DEM"): 1691156,
        ("Minogue and Oliver", "REP"): 930508,
        ("James and Kittredge", "IND"): 89951,
        ("Write-in", ""): 2496,
    },
    "MA-07": {("Ayanna S. Pressley", "DEM"): 185849, ("Write-in", ""): 5245},
}


def members():
    with zipfile.ZipFile(FIXTURE / "bundle.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def replaced(member, old, new):
    """A mutation replacing the first ``old`` in one CSV with ``new``."""

    def mutate(data):
        text = data[member].decode("utf-8")
        if old not in text:
            raise AssertionError(f"{old!r} not in {member}")
        data[member] = text.replace(old, new, 1).encode()

    return mutate


class MassachusettsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ma_2026_general"]

    def parse(self, mutate=None):
        data = members()
        if mutate:
            mutate(data)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.getvalue()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.getvalue())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_captured_files(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        data = members()
        self.assertEqual(set(data), set(ledger))
        self.assertEqual(set(data), set(self.source.expected_members))
        for name, entry in ledger.items():
            self.assertEqual(hashlib.sha256(data[name]).hexdigest(), entry["sha256"])
            identity = re.search(r"/download/(\d+)/", entry["url"])[1]
            configured = self.source.url if name == "senate.csv" else dict(self.source.files)[name]
            self.assertIn(f"/download/{identity}/precincts_include:0/", configured)

    def test_towns_add_up_to_the_totals_and_are_on_the_map(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            choice = (found["choice"], found["choice_party"])
            statewide.setdefault(found["contest_id"], {})[choice] = int(found["total_votes"])
        self.assertEqual(set(statewide), {"MA-SEN", "MA-GOV", *(f"MA-0{n}" for n in range(1, 10))})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        towns = self.read(report, "county_results.csv")
        senate = {r["county"] for r in towns if r["contest_id"] == "MA-SEN"}
        self.assertEqual(len(senate), 351)
        # Abbreviated directions are written out as the map writes them.
        self.assertIn("North Adams", senate)
        self.assertNotIn("N. Adams", senate)
        # A city split between districts is in each.
        for race in ("MA-07", "MA-08"):
            self.assertIn("Boston", {r["county"] for r in towns if r["contest_id"] == race})
        geo = json.loads((STATIC / "ma-map.json").read_text())
        keys = {re.sub(r"[^A-Z0-9]", "", t.upper()) for t in senate}
        self.assertEqual(keys, {f["id"] for f in geo["counties"]})
        self.assertEqual(len(geo["districts"]), 9)

    def test_changed_counts_candidates_or_layout_are_refused(self):
        changes = {
            # A town's vote that no longer adds up to its votes cast, or to the TOTALS row.
            "town": replaced("senate.csv", 'Abington,,,"3,858"', 'Abington,,,"3,859"'),
            "town and cast": replaced(
                "senate.csv", '157,136,3,161,"7,455"', '157,136,3,161,"7,456"'
            ),
            "totals": replaced("governor.csv", '"1,691,156"', '"1,691,157"'),
            "count": replaced("governor.csv", '"930,508"', '"93x,508"'),
            "party": replaced("house-04.csv", "Republican", "Libertarian"),
            "candidate": replaced("house-01.csv", "Richard E. Neal", "Richard Neal"),
            "header": replaced("house-05.csv", "All Others", "Others"),
            "duplicate town": replaced("house-07.csv", "Cambridge,", "Boston,"),
            "missing race": lambda data: data.pop("house-09.csv"),
        }
        for name, mutate in changes.items():
            with self.subTest(name):
                report = self.parse(mutate)
                self.assertEqual(report["outcome"], "validation_failed", report)


if __name__ == "__main__":
    unittest.main()
