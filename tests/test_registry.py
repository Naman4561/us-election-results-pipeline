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
RESOURCES = ROOT / "src/election_pipeline/resources"
# States whose parser checks a reviewed scope of its own instead of a roster file, and
# the file it checks.
OWN_SCOPE = {
    # Each race's candidates by ballot ID and party, and its reporting units.
    "AK": "ak_2026_scope.json",
    # Each district's candidates and parties, and each county's contract.
    "MS": "ms_2026_house.json",
}


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings, cls.sources = load_config(ROOT / "config")

    def test_each_state_has_a_source_parser_roster_and_map(self):
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertEqual(self.sources[state.source_id].state, code)
                self.assertTrue(callable(state.parser.parse))
                self.assertTrue((STATIC / f"{state.slug}-map.json").exists())
                # A new candidate, a missing county or a renamed choice must stop a parse.
                if code in OWN_SCOPE:
                    self.assertNotIn(code, ROSTERS)
                    self.assertTrue((RESOURCES / OWN_SCOPE[code]).is_file())
                else:
                    self.assertEqual(set(state.races), set(ROSTERS[code]))

    def test_each_roster_is_checked_by_its_parser(self):
        package = ROOT / "src/election_pipeline/parsers"
        text = {p.stem: p.read_text(encoding="utf-8") for p in package.glob("*.py")}

        def checks(source):
            return any(call in source for call in ("verify_scope(", "verify_rows(", "ROSTERS["))

        # Modules that check the roster for a parser calling them: the platform readers,
        # and a state's own readers (Kentucky's saved pages).
        readers = {name for name, source in text.items() if name != "scope" and checks(source)}
        for code, state in STATES.items():
            if code in OWN_SCOPE:
                continue
            with self.subTest(code):
                own = text[state.parser_module or state.slug]
                self.assertTrue(
                    checks(own)
                    or any(
                        f"parsers.{name} import" in own or f"parsers import {name}" in own
                        for name in readers
                    ),
                    "Its parser, or the platform reader it calls, must verify its roster",
                )

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
