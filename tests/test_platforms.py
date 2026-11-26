"""The platform settings an election definition gives each reader are checked when read."""

import copy
import unittest
from unittest import mock

from election_pipeline import elections, platforms
from election_pipeline.states import ELECTION

ROOT = elections.CONFIG.parent


def edited(code, change):
    """The election's platform tables with ``change`` applied to state ``code``'s."""
    tables = copy.deepcopy(ELECTION.platforms)
    change(tables[code])
    return mock.patch.dict(ELECTION.platforms, tables, clear=True)


class PlatformTests(unittest.TestCase):
    def source(self, code):
        return ELECTION.sources[ELECTION.states[code].source_id]

    def test_every_platform_state_reads_and_names_each_race_once(self):
        readers = {
            "tally": platforms.tally,
            "enhanced_voting": platforms.enhanced_voting,
            "clarity": platforms.clarity,
        }
        self.assertEqual(
            sorted(ELECTION.platforms),
            ["AR", "CO", "GA", "IA", "ID", "ND", "RI", "SC", "UT", "VA", "WA", "WV"],
        )
        for code, raw in ELECTION.platforms.items():
            with self.subTest(code):
                found = readers[raw["reader"]](self.source(code))
                self.assertEqual(set(found.titles.values()), set(ELECTION.states[code].races))

    def test_a_race_without_a_title_is_refused(self):
        def drop(table):
            table["titles"].pop("Governor")

        with edited("GA", drop), self.assertRaisesRegex(ValueError, "each of its races once"):
            platforms.enhanced_voting(self.source("GA"))

    def test_two_titles_for_one_race_are_refused(self):
        def twice(table):
            table["titles"]["Governor again"] = "GA-GOV"

        with edited("GA", twice), self.assertRaisesRegex(ValueError, "once"):
            platforms.enhanced_voting(self.source("GA"))

    def test_unknown_settings_and_modes_are_refused(self):
        def extra(table):
            table["anything"] = "1"

        def mode(table):
            table["county_names"] = "guess"

        with edited("VA", extra), self.assertRaisesRegex(ValueError, "holds"):
            platforms.enhanced_voting(self.source("VA"))
        with edited("VA", mode), self.assertRaisesRegex(ValueError, "county_names"):
            platforms.enhanced_voting(self.source("VA"))

    def test_a_table_for_another_reader_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not for the tally reader"):
            platforms.tally(self.source("GA"))

    def test_a_state_without_a_table_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no \\[platform\\] table"):
            platforms.tally(self.source("OK"))

    def test_the_county_mode_decides_how_localities_are_named(self):
        self.assertEqual(
            platforms.enhanced_voting(self.source("GA")).county("Fulton County"), "Fulton"
        )
        virginia = platforms.enhanced_voting(self.source("VA"))
        self.assertEqual(virginia.county("FAIRFAX COUNTY"), "FAIRFAX COUNTY")
        self.assertIsNone(platforms.enhanced_voting(self.source("RI")).county)
        with self.assertRaises(ValueError):
            platforms.enhanced_voting(self.source("GA")).county("Fulton")

    def test_clarity_titles_ignore_doubled_spaces_and_quote_folding_is_a_setting(self):
        south_carolina = platforms.clarity(self.source("SC"))
        self.assertEqual(south_carolina.race("U.S.  Senate"), "SC-SEN")
        self.assertEqual(south_carolina.race("U.S. House of Representatives, District  1"), "SC-01")
        self.assertEqual(south_carolina.workbook_race("U.S. Senate (Vote For 1)"), "SC-SEN")
        self.assertIsNone(south_carolina.workbook_race("U.S. Senate"))
        self.assertFalse(south_carolina.same_name("Mark 'Jr' Elworth", 'Mark "Jr" Elworth'))
        colorado = platforms.clarity(self.source("CO"))
        self.assertTrue(colorado.same_name("Mark 'Jr' Elworth", 'Mark "Jr" Elworth'))

    def test_a_clarity_detail_xml_needs_the_elections_name(self):
        def nameless(table):
            table.pop("election_name")

        with edited("CO", nameless), self.assertRaisesRegex(ValueError, "election's name"):
            platforms.clarity(self.source("CO"))

    def test_only_a_reviewed_party_is_read(self):
        found = platforms.tally(self.source("ND"))
        self.assertEqual(found.parties["Democratic-NPL"], "DEM")
        self.assertNotIn("Whig", found.parties)


if __name__ == "__main__":
    unittest.main()
