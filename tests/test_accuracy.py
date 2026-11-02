"""Reference completeness, evidence preservation and reporting limits."""

import csv
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from election_pipeline import accuracy
from election_pipeline.parsers.nc import COVERAGE


class AccuracyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.reference = self.root / "reference.csv"

        self.metadata = {
            "dataset": "election_results",
            "state": "NC",
            "election_date": "2026-11-03",
            "release_id": 8,
            "source_id": "nc_2026_general",
            "raw_sha256": "a" * 64,
        }

        self.rows = [
            {
                "contest_id": race,
                "choice": "Candidate A",
                "choice_party": "DEM",
                "total_votes": 20,
            }
            for race in COVERAGE.values()
        ]

        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.execute.return_value.fetchone.return_value = (
            self.metadata
        )
        self.conn.execute.return_value.fetchall.side_effect = (
            lambda: self.rows
        )

        self.enterContext(
            patch.object(
                accuracy, "connect", return_value=self.conn
            )
        )
        self.write_reference()

    def write_reference(self, omit=False):
        with self.reference.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ("election_date", "contest_id", "choice", "total_votes")
            )

            rows = self.rows[:-1] if omit else self.rows

            for row in rows:
                writer.writerow((
                    "2026-11-03",
                    row["contest_id"],
                    row["choice"],
                    row["total_votes"],
                ))

    def compare(self, **kwargs):
        return accuracy.compare_publication(
            {"password": "secret"},
            uuid.uuid4(),
            self.reference,
            evidence_dir=self.root / "evidence",
            reference_url="https://er.ncsbe.gov/",
            observed_at="2026-11-03T20:00:00-05:00",
            **kwargs,
        )

    def test_complete_agreement_preserves_frozen_evidence(self):
        result = self.compare(same_reporting_cutoff=True)
        self.assertEqual(result["outcome"], "matched")

        self.reference.write_text("changed after the check")

        captured = (
            Path(result["report_path"]).parent / "reference.csv"
        )
        self.assertEqual(
            accuracy.file_hash(captured),
            result["reference_sha256"],
        )

        saved = json.loads(
            Path(result["report_path"]).read_text()
        )
        self.assertEqual(saved["release_id"], 8)

    def test_missing_choice_and_wrong_total_cannot_pass(self):
        self.write_reference(omit=True)

        self.assertEqual(
            self.compare(
                same_reporting_cutoff=True
            )["outcome"],
            "mismatch",
        )

        self.write_reference()
        self.rows[0]["total_votes"] = 19

        self.assertEqual(
            self.compare(
                same_reporting_cutoff=True
            )["outcome"],
            "mismatch",
        )

    def test_matching_zero_data_is_not_live_accuracy_validation(self):
        for row in self.rows:
            row["total_votes"] = 0

        self.write_reference()

        self.assertEqual(
            self.compare(
                same_reporting_cutoff=True
            )["outcome"],
            "pre_reporting_only",
        )

    def test_cutoff_is_a_separate_manual_check(self):
        self.assertEqual(
            self.compare()["outcome"],
            "counts_match_cutoff_unverified",
        )