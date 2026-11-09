"""Reviewed roster data files and the roster review script."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from election_pipeline import pipeline
from election_pipeline.deployment.containers import package_hash
from election_pipeline.parsers.scope import recording, verify_scope
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "src/election_pipeline/resources/rosters_2026"
spec = importlib.util.spec_from_file_location("review", ROOT / "scripts/review_rosters.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class RosterTests(unittest.TestCase):
    def test_each_file_is_one_state_with_provenance_and_unique_entries(self):
        for path in sorted(FOLDER.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path.name, document["state"] + ".json")
            self.assertTrue(document["provenance"].strip())
            for race, entry in document["races"].items():
                self.assertTrue(race.startswith(document["state"] + "-"), race)
                self.assertEqual(len(entry["candidates"]), len(set(entry["candidates"])), race)
                self.assertEqual(len(entry["counties"]), len(set(entry["counties"])), race)
        self.assertEqual(set(ROSTERS), {p.stem for p in FOLDER.glob("*.json")})

    def test_roster_data_is_part_of_the_parser_code_hash(self):
        before = pipeline.collector_code_hash()
        self.assertEqual(package_hash(ROOT), before)
        # Change a copy: the installed package is read-only in the release image.
        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary)
            package = release / "src/election_pipeline"
            shutil.copytree(
                ROOT / "src/election_pipeline",
                package,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            self.assertEqual(pipeline.collector_code_hash(package), before)
            path = package / "resources/rosters_2026/HI.json"
            path.write_bytes(path.read_bytes() + b" ")
            self.assertNotEqual(pipeline.collector_code_hash(package), before)
            self.assertEqual(package_hash(release), pipeline.collector_code_hash(package))

    def test_recording_observes_without_checking_and_does_not_leak(self):
        with recording() as observed:
            verify_scope("HI", "HI-GOV", ["x", "y"], None)
        self.assertEqual(observed["HI"]["HI-GOV"]["candidates"], ["x", "y"])
        with self.assertRaises(ValueError):
            verify_scope("HI", "HI-GOV", ["x", "y"], None)

    def test_review_script_finds_no_change_for_the_reviewed_captures(self):
        settings, sources = load_config(ROOT / "config/sources.toml")
        for code in ("VA", "CO", "IA", "OR", "CA", "MD", "HI"):
            source = sources[code.lower() + "_2026_general"]
            archive = ROOT / f"tests/fixtures/nine_states/{code.lower()}.zip"
            observed = review.observe(source, settings, archive)
            self.assertEqual(review.differences(code, observed), {}, code)
        changed = {race: dict(entry) for race, entry in ROSTERS["HI"].items()}
        changed["HI-01"] = {**changed["HI-01"], "candidates": ["someone-new"]}
        diff = review.differences("HI", changed)
        self.assertEqual(diff["HI-01"]["candidates"]["added"], ["someone-new"])


if __name__ == "__main__":
    unittest.main()
