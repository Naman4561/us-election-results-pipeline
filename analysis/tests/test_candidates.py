"""Ballot names to FEC registrations. No database needed."""

import unittest

from analysis import candidates


def registration(cand_id, name, office="H", district="01"):
    return {"cand_id": cand_id, "name": name, "office": office, "district": district}


class CandidateMatchTests(unittest.TestCase):
    def found(self, ballot, registrations, funded=None):
        return [
            r["cand_id"] for r in candidates.match({"choice": ballot}, registrations, funded or {})
        ]

    def test_a_surname_alone_matches_within_a_race_whatever_the_first_name(self):
        race = [registration("H1", "JORDAN, JAMES D"), registration("H2", "SMITH, ANNA")]
        self.assertEqual(self.found("Jim Jordan", race), ["H1"])  # nickname
        self.assertEqual(
            self.found('Robert C. "Bobby" Scott', [registration("H3", "SCOTT, ROBERT C. MR.")]),
            ["H3"],
        )
        self.assertEqual(self.found("CASE, Ed", [registration("H4", "CASE, EDWARD")]), ["H4"])
        self.assertEqual(
            self.found("Jon Ossoff (I) (Dem)", [registration("S5", "OSSOFF, T. JONATHAN")]), ["S5"]
        )
        self.assertEqual(self.found("Nobody Here", race), [])

    def test_same_surname_in_one_race_is_settled_by_first_name_or_initial(self):
        race = [registration("H1", "JOHNSON, JAKE"), registration("H2", "JOHNSON, MARY")]
        self.assertEqual(self.found("Jake Johnson", race), ["H1"])
        self.assertEqual(self.found("M. Johnson", race), ["H2"])

    def test_one_committee_listed_under_two_ids_is_kept_once(self):
        race = [
            registration("H6NC09200", "HARRIS, MARK E"),
            registration("H4NC08066", "HARRIS, MARK E"),
        ]
        same = {"H6NC09200": (100, 90), "H4NC08066": (100, 90)}
        self.assertEqual(self.found("Mark Harris", race, same), ["H4NC08066"])
        different = {"H6NC09200": (100, 90), "H4NC08066": (5, 4)}
        self.assertEqual(len(self.found("Mark Harris", race, different)), 2)  # left for review

    def test_contest_ids_map_to_the_fec_office_and_district(self):
        # Kevin Hern's House and Senate registrations differ by office and district.
        self.assertEqual(candidates.office("OK-SEN"), ("Senate", "S", "00"))
        self.assertEqual(candidates.office("OK-01"), ("House", "H", "01"))
        self.assertEqual(candidates.office("WY-AL"), ("House", "H", "00"))
        self.assertEqual(candidates.office("OK-GOV"), ("Governor", None, None))

    def test_write_ins_are_not_candidates_and_party_labels_compare_loosely(self):
        for name in ("Write-In", "WRITE-IN", "Scattering", "Over Votes"):
            self.assertTrue(candidates.NOT_CANDIDATES.search(name))
        self.assertFalse(candidates.NOT_CANDIDATES.search("Mark Harris"))
        self.assertEqual(candidates.party("DFL"), candidates.party("DEM"))
        self.assertEqual(candidates.party("Republican"), "REP")


if __name__ == "__main__":
    unittest.main()
