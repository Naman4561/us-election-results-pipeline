"""A parse report read as typed facts: official status, report time, reconciliation."""

import unittest
from pathlib import Path

from election_pipeline.parse_report import (
    BASES,
    NOT_STATED,
    OFFICIAL,
    RECONCILIATION_OUTCOMES,
    UNOFFICIAL,
    Reading,
    fields,
    read,
    stated_time,
)


class OfficialStatusTests(unittest.TestCase):
    def test_a_source_that_says_nothing_is_not_stated_and_never_official(self):
        self.assertEqual(read({"official_results": True}).official, OFFICIAL)
        self.assertEqual(read({"official_results": False}).official, UNOFFICIAL)
        self.assertEqual(read({}).official, NOT_STATED)
        # Only a True the source gave counts: anything else cannot publish as official.
        for odd in ("true", 1, "official"):
            self.assertNotEqual(read({"official_results": odd}).official, OFFICIAL)

    def test_a_parser_whose_source_has_no_marker_says_so_and_is_still_unofficial(self):
        county_reports = {"official_results": False, "official_status": NOT_STATED}
        self.assertEqual(read(county_reports).official, NOT_STATED)
        # The flag consumers read stays False, so it can never publish as official.
        self.assertIsNot(county_reports["official_results"], True)
        self.assertEqual(
            read({"official_results": True, "official_status": OFFICIAL}).official, OFFICIAL
        )
        for report in (
            {"official_results": True, "official_status": NOT_STATED},
            {"official_results": False, "official_status": OFFICIAL},
            {"official_results": False, "official_status": "certified"},
            {"official_status": OFFICIAL},
        ):
            with self.subTest(report), self.assertRaisesRegex(ValueError, "Inconsistent"):
                read(report)


class DeclarationTests(unittest.TestCase):
    def test_a_parser_declares_its_sources_status_through_one_function(self):
        self.assertEqual(fields(True), {"official_results": True, "official_status": OFFICIAL})
        self.assertEqual(fields(False), {"official_results": False, "official_status": UNOFFICIAL})
        # A source with no marker is not stated, and the flag consumers read is False.
        self.assertEqual(
            fields(NOT_STATED), {"official_results": False, "official_status": NOT_STATED}
        )
        for status in (OFFICIAL, UNOFFICIAL, NOT_STATED):
            self.assertEqual(read(fields(status)).official, status)
        for odd in ("true", 1, None, "certified"):
            with self.subTest(odd), self.assertRaisesRegex(ValueError, "must be one of"):
                fields(odd)

    def test_no_parser_writes_the_status_flag_by_hand(self):
        folder = Path(__file__).resolve().parents[1] / "src/election_pipeline/parsers"
        writers = [
            path.name
            for path in sorted(folder.glob("*.py"))
            if "official_results=" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(writers, [])


class ReportTimeTests(unittest.TestCase):
    def test_a_time_is_iso_only_where_the_source_names_its_zone(self):
        for text, expected in (
            ("2026-11-09T21:11:09Z", "2026-11-09T21:11:09+00:00"),
            ("2026-11-04T03:03:18.364-05:00", "2026-11-04T03:03:18.364000-05:00"),
            ("11/04/2026 11:16:06 AM CT", "2026-11-04T11:16:06-06:00"),
            ("11/5/2026 8:04:54 PM MST", "2026-11-05T20:04:54-07:00"),
            ("11/5/2026 | 9:00 PM PT", "2026-11-05T21:00:00-08:00"),
            ("11/4/2026 3:05:06 PM EST", "2026-11-04T15:05:06-05:00"),
            ("11/4/2026 12:25:40 AM CST", "2026-11-04T00:25:40-06:00"),
            ("11/4/2026 12:25:40 PM CDT", "2026-11-04T12:25:40-05:00"),
            (
                "Last Update: 11/20/2026 10:42:18 AM EST; counties 120 / 120",
                "2026-11-20T10:42:18-05:00",
            ),
            # No zone, or one not known: left as text.
            ("2026-11-09T23:05:08.39", None),
            ("11/05/2026 10:56:33 PM", None),
            ("11/03/2026 8:41 PM", None),
            ("November 5, 2026, 6:58 p.m.", None),
            ("Nov 04, 2026 01:28:46", None),
            ("11/5/2026 9:18 AM XYZ", None),
            ("version 55530", None),
            ("", None),
            (None, None),
            (20261103, None),
        ):
            with self.subTest(text=text):
                self.assertEqual(stated_time(text), expected)

    def test_a_generic_zone_before_daylight_time_ended_is_not_guessed(self):
        # CT means CDT in the summer; the table knows standard time only.
        self.assertIsNone(stated_time("10/20/2026 9:00 AM CT"))
        self.assertEqual(stated_time("11/2/2026 9:00 AM CT"), "2026-11-02T09:00:00-06:00")
        # A named zone is exact whenever it is.
        self.assertEqual(stated_time("10/20/2026 9:00 AM CDT"), "2026-10-20T09:00:00-05:00")

    def test_where_the_time_comes_from_decides_its_basis(self):
        iso = {
            "source_generated_at": "2026-11-18T15:08:27-06:00",
            "source_update_text": "as of ...",
        }
        self.assertEqual(read(iso).reported_at_basis, "iso")
        self.assertEqual(read(iso).reported_at, "2026-11-18T15:08:27-06:00")
        zoned = read({"source_update_text": "11/6/2026 6:26:12 PM EST"})
        self.assertEqual(
            (zoned.reported_at_basis, zoned.reported_at),
            ("stated_zone", "2026-11-06T18:26:12-05:00"),
        )
        unzoned = read({"source_update_text": "11/05/2026 10:56:33 PM"})
        self.assertEqual((unzoned.reported_at_basis, unzoned.reported_at), ("no_zone", None))
        self.assertEqual(
            read({"source_timestamp": "11/5/2026 8:04:45 PM"}).reported_at_basis, "no_zone"
        )
        self.assertEqual(read({"source_update_times": {"MD-01": "x"}}).reported_at_basis, "no_zone")
        version = read({"source_update_text": "version 55530"})
        self.assertEqual(
            (version.reported_at_basis, version.reported_at_text), ("version", "version 55530")
        )
        self.assertEqual(read({}).reported_at_basis, "none")

    def test_the_reading_refuses_a_time_whose_zone_is_not_known(self):
        with self.assertRaisesRegex(ValueError, "exactly when its zone is known"):
            Reading(OFFICIAL, "t", "2026-11-05T21:00:00-08:00", "no_zone")
        with self.assertRaisesRegex(ValueError, "exactly when its zone is known"):
            Reading(OFFICIAL, "t", None, "iso")
        with self.assertRaisesRegex(ValueError, "basis must be one of"):
            Reading(OFFICIAL, None, None, "guess")
        with self.assertRaisesRegex(ValueError, "Official status"):
            Reading("certified", None, None, "none")
        self.assertEqual(set(BASES), {"iso", "stated_zone", "no_zone", "version", "none"})


class ReconciliationTests(unittest.TestCase):
    def test_every_reconciliation_the_parser_recorded_is_kept_with_a_known_outcome(self):
        found = read(
            {
                "county_reconciliation": "passed",
                "internal_reconciliation": "passed",
                "vote_method_reconciliation": "not_available",
                "contests": ["NC-SEN"],
                "county_counts": {"NC-SEN": 100},
            }
        ).reconciliation
        self.assertEqual(
            found,
            {
                "county_reconciliation": "passed",
                "internal_reconciliation": "passed",
                "vote_method_reconciliation": "not_available",
            },
        )
        with self.assertRaisesRegex(ValueError, "Unknown reconciliation outcomes"):
            read({"county_reconciliation": "mostly"})
        self.assertIn("passed_except_privacy_masked_counties", RECONCILIATION_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
