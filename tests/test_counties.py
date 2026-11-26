"""The county tables: what they hold, how a name resolves through them, and that they are
what ``scripts/build_county_tables.py`` writes from the pinned maps and reviewed rosters."""

import importlib.util
import json
import unittest
from pathlib import Path

from election_pipeline import counties, elections
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
FOLDER = elections.folder()


def generator():
    spec = importlib.util.spec_from_file_location(
        "build_county_tables", ROOT / "scripts/build_county_tables.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table_of(*places):
    return counties.CountyTable(
        {
            "state": "XX",
            "unit": "county",
            "counties": [
                {"fips": fips, "key": counties.key(name), "name": name, "spellings": spelled}
                for fips, name, spelled in places
            ],
        }
    )


class TableTests(unittest.TestCase):
    def test_every_state_that_publishes_places_has_a_table_and_the_others_have_none(self):
        for code, state in STATES.items():
            with self.subTest(code):
                exists = (FOLDER / "counties" / f"{code}.json").is_file()
                self.assertEqual(exists, state.county_mode != "none")

    def test_the_tables_are_what_the_generator_writes(self):
        build = generator()
        for code in build.states_with_places():
            with self.subTest(code):
                wanted = build.text(build.build(FOLDER, code))
                path = FOLDER / "counties" / f"{code}.json"
                self.assertEqual(path.read_bytes().decode("utf-8").replace("\r\n", "\n"), wanted)

    def test_fips_are_five_digits_for_counties_and_ten_for_towns_and_unique(self):
        for path in sorted((FOLDER / "counties").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(path.stem):
                width = 10 if record["unit"] == "town" else 5
                fips = [p["fips"] for p in record["counties"]]
                self.assertTrue(all(len(f) == width and f.isdigit() for f in fips))
                self.assertEqual(len(fips), len(set(fips)))
                self.assertEqual(record["state"], path.stem)
                counties.table(path.stem)  # loads: no spelling names two places

    def test_each_reviewed_roster_name_resolves_to_its_own_map_county(self):
        for code in ("KY", "NC", "NM", "FL", "VA", "SD"):
            roster = json.loads((FOLDER / "rosters" / f"{code}.json").read_text(encoding="utf-8"))
            names = {n for race in roster["races"].values() for n in race["counties"]}
            with self.subTest(code):
                self.assertTrue(names)
                for name in names:
                    self.assertIn(name, counties.table(code).exact)

    def test_a_spelling_the_table_lacks_falls_back_to_letters_and_digits(self):
        kentucky = counties.table("KY")
        self.assertEqual(kentucky.resolve("Ballard"), "21007")
        self.assertEqual(kentucky.resolve("BALLARD"), "21007")  # not listed: matched by letters
        self.assertEqual(counties.table("NM").resolve("Dona Ana"), "35013")
        with self.assertRaisesRegex(ValueError, "unknown county"):
            kentucky.resolve("Atlantis")


class ResolveTests(unittest.TestCase):
    def test_exact_spellings_win_and_the_fallback_needs_exactly_one_place(self):
        table = table_of(
            ("99001", "Saint Clair", ["Saint Clair", "St. Clair"]),
            ("99003", "Lee", ["Lee"]),
        )
        self.assertEqual(table.resolve("St. Clair"), "99001")
        self.assertEqual(table.resolve("SAINT CLAIR"), "99001")  # by letters and digits
        self.assertEqual(table.resolve("lee"), "99003")

    def test_names_that_are_unknown_or_ambiguous_are_refused(self):
        table = table_of(("99001", "Le Sueur", ["Le Sueur"]), ("99003", "LeSueur", ["LeSueur"]))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            table.resolve("LE-SUEUR")
        with self.assertRaisesRegex(ValueError, "unknown"):
            table.resolve("Elsewhere")

    def test_a_spelling_of_two_places_or_a_repeated_fips_is_refused_when_the_table_loads(self):
        with self.assertRaisesRegex(ValueError, "names two places"):
            table_of(("99001", "A", ["Same"]), ("99003", "B", ["Same"]))
        with self.assertRaisesRegex(ValueError, "listed twice"):
            table_of(("99001", "A", ["A"]), ("99001", "B", ["B"]))

    def test_a_state_without_a_table_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no county table"):
            counties.table("TX")


if __name__ == "__main__":
    unittest.main()
