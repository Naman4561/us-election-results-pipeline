"""ACS summary file values and geographies. No database or download needed."""

import unittest

from analysis import census


class CensusParsingTests(unittest.TestCase):
    def test_bureau_codes_are_not_numbers(self):
        self.assertEqual(census.estimate("1587"), "1587")
        self.assertEqual(census.estimate("0"), "0")
        for code in ("-666666666", "-999999999", "-888888888", "-222222222", "-333333333", ""):
            self.assertIsNone(census.estimate(code))
        # A controlled total has no sampling error; any other code has no margin to give.
        self.assertEqual(census.margin("-555555555"), "0")
        self.assertIsNone(census.margin("-222222222"))
        self.assertEqual(census.margin("466"), "466")

    def test_only_counties_and_current_districts_are_kept(self):
        self.assertEqual(census.COUNTY.fullmatch("0500000US40143")[1], "40143")
        self.assertEqual(census.DISTRICT.fullmatch("5001900US4001")[1], "4001")
        for other in ("0100000US", "0400000US40", "1400000US40143000100", "5001800US4001"):
            self.assertIsNone(census.COUNTY.fullmatch(other) or census.DISTRICT.fullmatch(other))


if __name__ == "__main__":
    unittest.main()
