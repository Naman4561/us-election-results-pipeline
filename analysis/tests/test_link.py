"""County spellings to FIPS, and the restore's dump check. No database needed:
python -m unittest discover -s analysis/tests -t ."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from analysis import link, restore

COUNTIES = [
    ("51159", "VA", "Richmond County"),
    ("51760", "VA", "Richmond city"),
    ("35013", "NM", "Doña Ana County"),
    ("40079", "OK", "Le Flore County"),
    ("29186", "MO", "Ste. Genevieve County"),
    ("29510", "MO", "St. Louis city"),
    ("29189", "MO", "St. Louis County"),
    ("22033", "LA", "East Baton Rouge Parish"),
]


class CountyMatchTests(unittest.TestCase):
    def test_spellings_match_exactly_or_normalized_and_cities_stay_cities(self):
        spellings = [
            ("VA", "Richmond County"), ("VA", "RICHMOND CITY"), ("VA", "Richmond"),
            ("NM", "DONA ANA"), ("OK", "LEFLORE"), ("MO", "Ste Genevieve"),
            ("MO", "Saint Louis City"), ("LA", "East Baton Rouge"),
        ]  # fmt: skip
        found, missing = link.matches(spellings, COUNTIES)
        self.assertEqual(missing, [])
        self.assertEqual(
            {key: fips for key, (fips, _) in found.items()},
            {
                ("VA", "Richmond County"): "51159",
                ("VA", "RICHMOND CITY"): "51760",
                ("VA", "Richmond"): "51159",
                ("NM", "DONA ANA"): "35013",
                ("OK", "LEFLORE"): "40079",
                ("MO", "Ste Genevieve"): "29186",
                ("MO", "Saint Louis City"): "29510",
                ("LA", "East Baton Rouge"): "22033",
            },
        )
        self.assertEqual(found["VA", "Richmond County"][1], "exact")
        self.assertEqual(found["NM", "DONA ANA"][1], "normalized")

    def test_unknown_or_other_state_spellings_are_reported_and_reviewed_rows_win(self):
        found, missing = link.matches([("VA", "Atlantis"), ("OK", "Richmond")], COUNTIES)
        self.assertEqual((found, missing), ({}, [("VA", "Atlantis"), ("OK", "Richmond")]))
        found, _ = link.matches([("VA", "Atlantis")], COUNTIES, {("VA", "Atlantis"): "51159"})
        self.assertEqual(found, {("VA", "Atlantis"): ("51159", "reviewed")})

    def test_vermont_towns_are_not_matched_as_counties(self):
        self.assertNotIn("VT", link.by_county())
        self.assertIn("OK", link.by_county())


class DumpCheckTests(unittest.TestCase):
    def test_a_dump_must_match_the_manifest_size_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "database.dump"
            path.write_bytes(b"PGDMP")
            good = {"bytes": 5, "sha256": hashlib.sha256(b"PGDMP").hexdigest()}
            self.assertTrue(restore.verified(path, good))
            self.assertFalse(restore.verified(path, {**good, "bytes": 6}))
            self.assertFalse(restore.verified(path, {**good, "sha256": "0" * 64}))


if __name__ == "__main__":
    unittest.main()
