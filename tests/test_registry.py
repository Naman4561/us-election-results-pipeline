"""Every registered state is wired up the same way.

These checks run over ``STATES`` rather than one cohort, so a newly registered state is
covered without copying them. Cohort tests keep what is specific to their captures.
"""

import unittest
from pathlib import Path

from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.settings import load_config
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/election_pipeline/dashboard/static"


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings, cls.sources = load_config(ROOT / "config/sources.toml")

    def test_each_state_has_a_source_parser_roster_and_map(self):
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertEqual(self.sources[state.source_id].state, code)
                self.assertTrue(callable(state.parser.parse))
                self.assertTrue((STATIC / f"{state.slug}-map.json").exists())
                if code in ROSTERS:
                    self.assertEqual(set(state.races), set(ROSTERS[code]))

    def test_every_configured_source_belongs_to_one_state(self):
        results = {state.source_id for state in STATES.values()}
        for name, source in self.sources.items():
            with self.subTest(name):
                adapter = STATES[source.state]
                if source.dataset == "election_results":
                    self.assertEqual(name, adapter.source_id)
                else:
                    self.assertIn(source.dataset, adapter.datasets)
                    self.assertNotIn(name, results)

    def test_races_cover_every_office_the_state_elects(self):
        for code, state in STATES.items():
            with self.subTest(code):
                races = list(state.races)
                self.assertEqual(len(races), len(set(races)))
                self.assertEqual(any(r.endswith("-SEN") for r in races), state.senate)
                self.assertEqual(any(r.endswith("-GOV") for r in races), state.governor)
                self.assertEqual(
                    sum(r[3:].isdigit() or r.endswith("-AL") for r in races), len(state.house)
                )


if __name__ == "__main__":
    unittest.main()
