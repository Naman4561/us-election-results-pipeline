"""Vote methods and special races are the election definition's, not the schema's (step 3.6)."""

import csv
import dataclasses
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_pipeline import elections
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.structured import row
from election_pipeline.settings import load_config
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
SETTINGS, SOURCES = load_config(ROOT / "config")


def copied_config(directory):
    config = Path(directory) / "config"
    shutil.copytree(elections.CONFIG, config)
    return config


class MethodVocabularyTests(unittest.TestCase):
    def test_every_state_method_is_in_the_vocabulary_and_each_name_has_the_schemas_shape(self):
        vocabulary = elections.methods()
        self.assertIn("total_votes", vocabulary)
        for name in vocabulary:
            self.assertRegex(name, r"^[a-z][a-z0-9_]{1,40}$")
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertLessEqual(set(state.methods), set(vocabulary))

    def test_a_state_naming_a_method_the_vocabulary_lacks_is_refused_when_the_definition_loads(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            path = config / "elections/2026-general/states/IA.toml"
            text = path.read_text(encoding="utf-8")
            self.assertIn('"absentee_combined"', text)
            path.write_text(
                text.replace('"absentee_combined"', '"absentee_novel"'), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, r"absentee_novel.*not in methods\.toml"):
                elections.load(config=config)

    def test_the_new_method_loads_once_the_vocabulary_names_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            path = config / "elections/2026-general/states/IA.toml"
            path.write_text(
                path.read_text(encoding="utf-8").replace('"absentee_combined"', '"absentee_novel"'),
                encoding="utf-8",
            )
            vocabulary = config / "methods.toml"
            vocabulary.write_text(
                vocabulary.read_text(encoding="utf-8")
                + '\n[absentee_novel]\nmeaning = "A group a new state publishes."\n',
                encoding="utf-8",
            )
            self.assertIn("absentee_novel", elections.load(config=config).states["IA"].methods)

    def test_a_method_without_a_meaning_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            vocabulary = config / "methods.toml"
            vocabulary.write_text(
                vocabulary.read_text(encoding="utf-8") + '\n[nameless]\nmeaning = ""\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "needs a meaning"):
                elections.methods(config)


class SpecialRaceTests(unittest.TestCase):
    def special(self, code="GA", suffix="SEN-S"):
        return dataclasses.replace(
            STATES[code], specials=((suffix, "U.S. Senate (special)", "Special"),)
        )

    def test_a_special_race_is_listed_beside_the_regular_ones_and_is_always_required(self):
        georgia = self.special()
        self.assertIn("GA-SEN-S", georgia.races)
        self.assertIn("GA-SEN", georgia.races)
        self.assertIn("GA-SEN-S", georgia.base_races)
        # A state whose House is optional still needs its special in every release.
        mississippi = self.special("MS")
        self.assertIn("MS-SEN-S", mississippi.base_races)
        self.assertNotIn("MS-01", mississippi.base_races)
        self.assertTrue(mississippi.accepts(mississippi.base_races))

    def test_no_state_in_2026_has_one_and_its_races_are_unchanged(self):
        for code, state in STATES.items():
            with self.subTest(code):
                self.assertEqual(state.specials, ())
                self.assertEqual(
                    state.base_races == state.races,
                    not (state.optional_house or state.collected_counties),
                )

    def test_a_special_race_must_be_a_suffix_of_a_seat_and_listed_once(self):
        for bad in ("SEN", "S", "SEN-", "sen-s", "SEN-TOOLONG", "XYZ-S"):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "special race"):
                self.special(suffix=bad)
        with self.assertRaisesRegex(ValueError, "listed twice"):
            dataclasses.replace(
                STATES["GA"], specials=(("SEN-S", "A", "Special"), ("SEN-S", "B", "Special"))
            )

    def test_the_definition_lists_a_special_race_under_races(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copied_config(directory)
            path = config / "elections/2026-general/states/GA.toml"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(
                    "\n[acquisition]",
                    '\n[races.special]\n"SEN-S" = "U.S. Senate (special)"\n\n[acquisition]',
                    1,
                ),
                encoding="utf-8",
            )
            georgia = elections.load(config=config).states["GA"]
            self.assertEqual(georgia.specials, (("SEN-S", "U.S. Senate (special)", "Special"),))
            self.assertIn("GA-SEN-S", georgia.races)

    def test_finish_writes_a_special_race_beside_the_regular_one(self):
        delaware = self.special("DE", "SEN-S")
        source = SOURCES["de_2026_general"]
        counties = ("Kent", "New Castle", "Sussex")
        rows = []
        for race in delaware.races:
            for county in counties:
                for name, party, votes in (("A", "DEM", 7), ("B", "REP", 5)):
                    rows.append(
                        row(
                            race,
                            race,
                            f"{race}-{name}",
                            f"{race} {name}",
                            party,
                            votes,
                            county=county,
                        )
                    )
        with tempfile.TemporaryDirectory() as directory, patch.dict(STATES, {"DE": delaware}):
            output = Path(directory)
            stats = {}
            finish(rows, source, SETTINGS, output, stats)
            with (output / "contest_results.csv").open(encoding="utf-8", newline="") as handle:
                contests = {r["contest_id"]: r for r in csv.DictReader(handle)}
        self.assertIn("DE-SEN-S", contests)
        self.assertEqual(stats["contest_count"], len(delaware.races))
        self.assertTrue(
            re.fullmatch(r"[A-Z]{2}-(SEN|GOV|AL|[0-9]{2})(-[A-Z0-9]{1,4})?", "DE-SEN-S")
        )


if __name__ == "__main__":
    unittest.main()
