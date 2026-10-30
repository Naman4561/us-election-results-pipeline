"""Known-answer tests for parsing, reconciliation, rejection, and replay."""

import csv
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from election_pipeline.cli import main
from election_pipeline.parsers.nc import COVERAGE, RESULT_HEADER
from election_pipeline.parsing import parse_source
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.storage import ArchiveStore, file_hash


class ParsingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = CollectorSettings()

        self.source = Source(
            "nc_test",
            "NC",
            "2026-11-03",
            "https://example.com/results.zip",
            expected_members=("results.txt",),
        )

        self.rows = [
            [
                "WAKE", "11/03/2026", precinct, "1383", "S", contest,
                "Candidate A", "DEM", "1", "2", "3", "4", "1", "10", real,
            ]
            for contest in COVERAGE
            for precinct, real in (("001", "Y"), ("ABSENTEE", "N"))
        ]

    def archive(self, rows=None):
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t")
        writer.writerow(RESULT_HEADER)
        writer.writerows(self.rows if rows is None else rows)

        path = self.root / "input.zip"

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("results.txt", output.getvalue())

        return self.install(path)

    def install(self, path):
        digest = file_hash(path)
        store = ArchiveStore(self.root, self.source)
        target = store.archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        store.write_state({"url": self.source.url, "sha256": digest})
        return target

    def parse(self, **kwargs):
        return parse_source(self.source, self.settings, self.root, **kwargs)

    def read_output(self, result, name):
        with (Path(result["output_dir"]) / name).open(
            newline="", encoding="utf-8"
        ) as handle:
            return list(csv.DictReader(handle))

    def test_known_totals_and_administrative_units(self):
        self.archive()
        result = self.parse()

        self.assertEqual(result["outcome"], "parsed")
        self.assertEqual(result["internal_reconciliation"], "passed")
        self.assertEqual(result["reference_comparison"]["status"], "not_provided")
        self.assertEqual(result["administrative_rows_retained"], 5)

        rows = self.read_output(result, "precinct_results.csv")
        self.assertEqual(rows[0]["precinct"], "001")

        for name in ("county_results.csv", "contest_results.csv"):
            totals = self.read_output(result, name)
            self.assertEqual(len(totals), 5)
            self.assertEqual({row["total_votes"] for row in totals}, {"20"})
            self.assertEqual({row["early_voting"] for row in totals}, {"6"})

    def test_bad_records_are_rejected_without_publishing(self):
        cases = [
            (9, "-1"),
            (9, "1.0"),
            (13, "11"),
            (1, "11/03/2024"),
            (14, "X"),
            (8, "2"),
        ]

        for column, value in cases:
            with self.subTest(column=column, value=value):
                rows = [row.copy() for row in self.rows]
                rows[0][column] = value
                self.archive(rows)
                result = self.parse()

                self.assertEqual(result["outcome"], "validation_failed")
                self.assertTrue(Path(result["report_path"]).exists())

        self.assertEqual(list(self.root.glob("parsed/*/*/report.json")), [])

    def test_duplicates_and_missing_contests_are_rejected(self):
        for rows in (self.rows + [self.rows[0]], self.rows[:-2]):
            self.archive(rows)
            self.assertEqual(self.parse()["outcome"], "validation_failed")

    def test_zero_file_is_not_marked_as_live(self):
        rows = [row.copy() for row in self.rows]

        for row in rows:
            row[9:14] = ["0"] * 5

        self.archive(rows)
        result = self.parse()

        self.assertTrue(result["all_counts_zero"])
        self.assertEqual(result["election_status"], "not determined by parser")

    def test_replay_and_corruption_detection(self):
        raw = self.archive()
        first = self.parse()

        self.assertEqual(self.parse()["outcome"], "already_parsed")
        self.assertEqual(
            self.parse(sha256=file_hash(raw))["output_dir"],
            first["output_dir"],
        )

        (Path(first["output_dir"]) / "county_results.csv").write_text("corrupt")
        self.assertEqual(self.parse()["outcome"], "validation_failed")

        raw.write_bytes(b"corrupt raw")
        self.assertIn("SHA-256", self.parse()["error"])

    def test_supplied_reference_must_cover_every_choice(self):
        self.archive()
        reference = self.root / "reference.csv"

        def write_reference(value, omit=False):
            with reference.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ("election_date", "contest_id", "choice", "total_votes")
                )

                contests = list(COVERAGE.values())[:-1] if omit else COVERAGE.values()

                for contest in contests:
                    writer.writerow(("2026-11-03", contest, "Candidate A", value))

        write_reference(20)
        self.assertEqual(
            self.parse(reference=reference)["reference_comparison"]["status"],
            "matched",
        )

        write_reference(19)
        self.assertEqual(
            self.parse(reference=reference)["reference_comparison"]["status"],
            "mismatch",
        )

        write_reference(20, omit=True)
        self.assertEqual(
            self.parse(reference=reference)["reference_comparison"]["status"],
            "mismatch",
        )

    def test_corrections_can_reduce_counts_without_adding_snapshots(self):
        self.archive()
        first = self.parse()

        rows = [row.copy() for row in self.rows]
        rows[0][9], rows[0][13] = "1", "9"
        self.archive(rows)
        corrected = self.parse()

        self.assertEqual(corrected["outcome"], "parsed")
        self.assertNotEqual(corrected["output_dir"], first["output_dir"])

        totals = self.read_output(corrected, "contest_results.csv")
        senate = next(row for row in totals if row["contest_id"] == "NC-SEN")

        self.assertEqual(senate["total_votes"], "19")
        self.assertTrue(Path(first["output_dir"]).exists())

    def test_demographics_remain_separate(self):
        self.source = replace(
            self.source,
            format="csv",
            expected_members=(),
            dataset="absentee_early_demographics",
            expected_columns=(
                "county_name", "abs_request_type", "party_cd",
                "gender", "race", "ethnicity", "0",
            ),
        )

        path = self.root / "input.csv"
        path.write_text(
            "county_name,abs_request_type,party_cd,gender,race,ethnicity,0\n"
            "WAKE,ONE-STOP,DEM,FEMALE,ASIAN,UNDESIGNATED,7\n"
            "DURHAM,ONE-STOP,DEM,FEMALE,ASIAN,UNDESIGNATED,3\n",
            encoding="utf-8",
        )

        self.install(path)
        result = self.parse()

        self.assertEqual(result["outcome"], "parsed")

        totals = self.read_output(result, "state_demographics.csv")
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals[0]["ballot_count"], "10")
        self.assertIn("party_registration", totals[0])

        self.assertFalse(
            (Path(result["output_dir"]) / "contest_results.csv").exists()
        )

    def test_cli_reference_mismatch_has_nonzero_exit_status(self):
        self.archive()
        config = self.root / "sources.toml"

        config.write_text(
            '[sources.nc_test]\nstate="NC"\nelection_date="2026-11-03"\n'
            'url="https://example.com/results.zip"\n'
            'expected_members=["results.txt"]\n',
            encoding="utf-8",
        )

        arguments = [
            "parse",
            "--source", "nc_test",
            "--config", str(config),
            "--data-dir", str(self.root),
        ]

        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments), 0)

        reference = self.root / "reference.csv"

        with reference.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ("election_date", "contest_id", "choice", "total_votes")
            )

            for contest in COVERAGE.values():
                writer.writerow(("2026-11-03", contest, "Candidate A", "19"))

        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(
                main([*arguments, "--reference", str(reference)]),
                1,
            )

        self.assertEqual(
            json.loads(captured.getvalue())["reference_comparison"]["status"],
            "mismatch",
        )


if __name__ == "__main__":
    unittest.main()