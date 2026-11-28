"""Runoffs and uncontested seats are declared in the election definition (step 3.8)."""

import dataclasses
import json
import tempfile
import unittest

from test_vocabularies import copied_config

from election_pipeline import elections
from election_pipeline.states import STATES

try:
    from election_pipeline.dashboard import server
    from election_pipeline.dashboard.server import STATIC, make_payload
except ImportError:
    server = None

SEAT = ("10", "Pat Example", "DEM", "Unopposed, so not on the ballot.", "https://example.org/list")


def with_seat(code="AL", *seats):
    return dataclasses.replace(STATES[code], uncontested=seats)


class ModelTests(unittest.TestCase):
    def test_floridas_district_10_is_a_declared_seat_with_no_race(self):
        florida = STATES["FL"]
        self.assertNotIn("FL-10", florida.races)
        self.assertEqual(len(florida.races), 29)
        seat = florida.uncontested_races["FL-10"]
        self.assertEqual(seat["winner"], "Maxwell Alejandro Frost")
        self.assertEqual(seat["party"], "DEM")
        self.assertTrue(seat["source"].startswith("https://dos.elections.myflorida.com/"))
        # The basis says what the statute and the Division say, not more.
        self.assertIn("101.151(7)", seat["basis"])
        self.assertNotIn("without a vote", seat["basis"])
        self.assertEqual({c for c, s in STATES.items() if s.uncontested}, {"FL"})

    def test_louisianas_runoffs_come_from_its_definition(self):
        self.assertEqual(STATES["LA"].runoff_dates, {"LA-05": "2026-12-12", "LA-06": "2026-12-12"})
        self.assertEqual({c for c, s in STATES.items() if s.runoffs}, {"LA"})

    def test_an_uncontested_seat_needs_everything_and_must_not_be_a_contested_race(self):
        self.assertEqual(with_seat("AL", SEAT).uncontested_races["AL-10"]["winner"], "Pat Example")
        for bad in (
            ("1", *SEAT[1:]),  # not a two-digit district
            ("XX", *SEAT[1:]),
            ("05", *SEAT[1:]),  # a contested House race of the state
            ("10", "", *SEAT[2:]),  # no winner
            ("10", SEAT[1], "Dem", *SEAT[3:]),  # not a party code
            ("10", *SEAT[1:3], "", SEAT[4]),  # no basis
            ("10", *SEAT[1:4], "http://example.org"),  # source not https
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                with_seat("AL", bad)
        with self.assertRaisesRegex(ValueError, "listed twice"):
            with_seat("AL", SEAT, SEAT)

    def test_a_runoff_must_be_a_race_of_the_state_on_a_real_date(self):
        alabama = STATES["AL"]
        for bad in (("99", "2026-12-12"), ("05", "12/12/2026"), ("05", "2026-13-40")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dataclasses.replace(alabama, runoffs=(bad,))
        self.assertEqual(
            dataclasses.replace(alabama, runoffs=(("05", "2026-12-12"),)).runoff_dates,
            {"AL-05": "2026-12-12"},
        )

    def test_the_definition_refuses_an_uncontested_seat_with_a_missing_or_extra_field(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            path = config / "elections/2026-general/states/FL.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace('party = "DEM"\n', ""), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "winner, party, basis and source"):
                elections.load(config=config)


@unittest.skipUnless(server, "Install postgres, dashboard extras and httpx")
class PayloadTests(unittest.TestCase):
    def payload(self, code):
        state = STATES[code]
        geography = json.loads((STATIC / f"{state.slug}-map.json").read_text(encoding="utf-8"))
        county = geography["counties"][0]
        rows = [
            {
                "county": county["name"],
                "county_fips": county["fips"],
                "contest_id": race,
                "choice": f"{race} {name}",
                "choice_party": party,
                "total_votes": votes,
                # All the votes in the first listed method; the rest are none.
                **{m: votes if i == 0 else 0 for i, m in enumerate(state.methods[1:])},
            }
            for race in state.races
            for name, party, votes in (("A", "DEM", 7), ("B", "REP", 5))
        ]
        return make_payload(geography, {}, rows, rows, state=code)

    def test_floridas_payload_lists_its_uncontested_seat_beside_its_races(self):
        payload = self.payload("FL")
        self.assertEqual(payload["uncontested"], STATES["FL"].uncontested_races)
        self.assertNotIn("FL-10", payload["races"])
        self.assertEqual(len(payload["races"]), 29)

    def test_a_state_with_none_has_no_such_key_and_louisianas_runoffs_are_its_own(self):
        self.assertNotIn("uncontested", self.payload("NC"))
        races = self.payload("LA")["races"]
        self.assertEqual(races["LA-05"]["runoff"], "2026-12-12")
        self.assertEqual(races["LA-06"]["runoff"], "2026-12-12")
        self.assertNotIn("runoff", races["LA-01"])

    def test_the_server_no_longer_carries_election_facts(self):
        self.assertFalse(hasattr(server, "RUNOFFS"))
        self.assertFalse(hasattr(server, "METHODS"))


if __name__ == "__main__":
    unittest.main()
