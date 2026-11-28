"""A seat held more than once (a special election, a runoff) is shown as tabs (step 3.6)."""

import dataclasses
import json
import tempfile
import unittest
from unittest.mock import patch

from test_vocabularies import copied_config

from election_pipeline import elections
from election_pipeline.states import STATES

try:
    from election_pipeline.dashboard.server import STATIC, make_payload
except ImportError:
    make_payload = None


def with_races(code, *specials):
    return dataclasses.replace(STATES[code], specials=specials)


class VariantTests(unittest.TestCase):
    def test_a_seat_with_one_race_has_no_tabs(self):
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertEqual(state.variants, {})

    def test_a_special_beside_the_regular_seat_is_two_tabs(self):
        georgia = with_races("GA", ("SEN-S", "U.S. Senate (special)", "Special"))
        self.assertEqual(
            georgia.variants,
            {"GA-SEN": ("GA-SEN", "Regular"), "GA-SEN-S": ("GA-SEN", "Special")},
        )

    def test_a_runoff_makes_the_first_round_the_general_tab(self):
        louisiana = with_races("LA", ("05-R", "U.S. House · LA-05 runoff", "Runoff"))
        self.assertEqual(
            louisiana.variants,
            {"LA-05": ("LA-05", "General"), "LA-05-R": ("LA-05", "Runoff")},
        )
        self.assertIn("LA-05-R", louisiana.races)

    def test_two_seats_each_with_extra_races_stay_apart(self):
        state = with_races(
            "GA",
            ("SEN-S", "Special Senate", "Special"),
            ("GOV-R", "Governor runoff", "Runoff"),
        )
        self.assertEqual(state.variants["GA-GOV"], ("GA-GOV", "General"))
        self.assertEqual(state.variants["GA-SEN"], ("GA-SEN", "Regular"))
        self.assertEqual(len({seat for seat, _ in state.variants.values()}), 2)

    def test_the_definition_gives_a_race_its_tab_or_defaults_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            path = config / "elections/2026-general/states/GA.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "\n[acquisition]",
                    '\n[races.special]\n"SEN-S" = "Special Senate"\n'
                    '"GOV-R" = { name = "Governor runoff", tab = "Runoff" }\n\n[acquisition]',
                    1,
                ),
                encoding="utf-8",
            )
            georgia = elections.load(config=config).states["GA"]
        self.assertEqual(
            georgia.specials,
            (("GOV-R", "Governor runoff", "Runoff"), ("SEN-S", "Special Senate", "Special")),
        )

    def test_a_special_race_with_an_unknown_setting_is_refused(self):
        with self.assertRaisesRegex(ValueError, "name and an optional tab"):
            elections.special_races({"SEN-S": {"name": "Senate", "colour": "red"}}, "GA.toml")


@unittest.skipUnless(make_payload, "Install postgres, dashboard extras and httpx")
class VariantPayloadTests(unittest.TestCase):
    def rows(self, state, races):
        geography = json.loads((STATIC / f"{state.slug}-map.json").read_text(encoding="utf-8"))
        county = geography["counties"][0]
        out = []
        for race in races:
            for name, party, votes in (("A", "DEM", 7), ("B", "REP", 5)):
                row = {
                    "county": county["name"],
                    "county_fips": county["fips"],
                    "contest_id": race,
                    "choice": f"{race} {name}",
                    "choice_party": party,
                    "total_votes": votes,
                }
                out.append(row)
        return geography, out

    def test_each_race_of_a_seat_says_its_seat_and_its_tab(self):
        delaware = with_races("DE", ("SEN-S", "U.S. Senate (special)", "Special"))
        geography, rows = self.rows(delaware, delaware.races)
        with patch.dict(STATES, {"DE": delaware}):
            payload = make_payload(geography, {}, rows, rows, state="DE")
        races = payload["races"]
        self.assertEqual(races["DE-SEN"]["variant"], {"seat": "DE-SEN", "tab": "Regular"})
        self.assertEqual(races["DE-SEN-S"]["variant"], {"seat": "DE-SEN", "tab": "Special"})
        self.assertNotIn("variant", races[next(r for r in races if r not in delaware.variants)])


if __name__ == "__main__":
    unittest.main()
