"""Every registered state is wired up the same way.

These checks run over ``STATES`` rather than one cohort, so a newly registered state is
covered without copying them. Cohort tests keep what is specific to their captures.
"""

import importlib.util
import unittest
from pathlib import Path

from election_pipeline.deployment import migrations
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.settings import load_config
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/election_pipeline/dashboard/static"
spec = importlib.util.spec_from_file_location("new_state", ROOT / "scripts/new_state.py")
new_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(new_state)


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings, cls.sources = load_config(ROOT / "config/sources.toml")

    def test_generated_dbt_files_and_migrations_match_the_registry(self):
        codes = [code for code, state in STATES.items() if new_state.generated(state)]
        self.assertEqual(new_state.drift(codes), [], "run: python scripts/new_state.py <STATE>")

    def test_each_state_has_a_source_parser_roster_and_map(self):
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertEqual(self.sources[state.source_id].state, code)
                self.assertTrue(callable(state.parser.parse))
                self.assertTrue((STATIC / f"{state.schema}-map.json").exists())
                if code in ROSTERS:
                    self.assertEqual(set(state.races), set(ROSTERS[code]))
                # A state's own house list must sit inside the offices it can select.
                self.assertLessEqual(state.races.keys(), state.available_races.keys())

    def test_each_state_migration_is_additive_and_scoped_to_one_state(self):
        for code, state in STATES.items():
            found = sorted(ROOT.glob(f"migrations/[0-9][0-9][0-9]_{state.schema}.sql"))
            if not found:
                continue  # Created with other states in 003 or 004.
            with self.subTest(code):
                body = migrations.validate_migration(found[0].read_text(), state.schema)
                quoted = f'"{state.schema}"' if state.schema in new_state.RESERVED else state.schema
                self.assertIn(f"CREATE TABLE {quoted}.results", body)
                if not state.custom_models:
                    self.assertEqual(
                        f"{quoted}.county_candidate_results" in body, state.county_results
                    )
                    for method in state.methods[1:]:
                        self.assertIn(f"{method} bigint NOT NULL", body)


if __name__ == "__main__":
    unittest.main()
