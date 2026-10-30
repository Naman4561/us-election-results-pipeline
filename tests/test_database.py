"""Artifact tests and integration tests in a disposable *_test database."""

import csv
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import test_parsing as fixtures

try:
    import psycopg

    from election_pipeline.database import (
        connect,
        connection_settings,
        converted_rows,
        load_parsed,
        verify,
    )
except ImportError:
    psycopg = None

from election_pipeline.storage import ArchiveStore, file_hash, utc_now


@unittest.skipUnless(psycopg, "Install the postgres optional dependencies")
class DatabaseArtifactTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ParsingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.raw = self.f.archive()
        self.directory = Path(self.f.parse()["output_dir"])
        self.store = ArchiveStore(self.f.root, self.f.source)

    def verified(self):
        return verify(
            self.f.source,
            self.f.settings,
            self.store,
            self.directory,
        )

    def test_verified_records_have_database_types(self):
        report, _, _, table, name, fields = self.verified()

        rows = list(
            converted_rows(
                self.directory / name,
                fields,
                self.f.source,
                1,
                report["output_rows"],
            )
        )

        self.assertEqual(table, "nc_results")
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0][3], "001")
        self.assertIs(rows[0][11], True)
        self.assertEqual(rows[0][-1], 10)

    def test_tampered_artifacts_are_rejected_before_database_copy(self):
        path = self.directory / "precinct_results.csv"
        path.write_bytes(path.read_bytes() + b"\n")

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.verified()

    def test_changed_configuration_requires_reparse(self):
        source = replace(self.f.source, min_interval_seconds=300)

        with self.assertRaisesRegex(ValueError, "configuration changed"):
            verify(
                source,
                self.f.settings,
                ArchiveStore(self.f.root, source),
                self.directory,
            )

    def test_detailed_rows_cannot_exceed_declared_count(self):
        _, _, _, _, name, fields = self.verified()

        with self.assertRaisesRegex(ValueError, "expected row count"):
            list(
                converted_rows(
                    self.directory / name,
                    fields,
                    self.f.source,
                    1,
                    9,
                )
            )


@unittest.skipUnless(
    psycopg and os.getenv("ELECTION_TEST_DATABASE"),
    "Set ELECTION_TEST_DATABASE to a disposable database ending in _test",
)
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ["ELECTION_TEST_DATABASE"]

        if not name.endswith("_test"):
            raise ValueError(
                "Integration tests require a database name ending in _test"
            )

        cls.db = {**connection_settings(), "dbname": name}

        with connect(cls.db) as conn:
            actual = conn.execute(
                "SELECT current_database() AS name"
            ).fetchone()["name"]

            if actual != name:
                raise ValueError("Unexpected test database")

            exists = conn.execute(
                "SELECT to_regclass('ops.sources') AS t"
            ).fetchone()["t"]

            if exists is None:
                migration = (
                    Path(__file__).resolve().parents[1]
                    / "migrations/001_initial.sql"
                )
                conn.execute(migration.read_text(encoding="utf-8"))

    def setUp(self):
        self.fixture = fixtures.ParsingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

        self.fixture.source = replace(
            self.fixture.source,
            source_id="test_" + uuid.uuid4().hex,
        )

        # Only the explicitly selected disposable database is reset.
        with connect(self.db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")

    def query(self, statement, parameters=()):
        with connect(self.db) as conn:
            return conn.execute(statement, parameters).fetchall()

    def parsed(self, rows=None):
        raw = self.fixture.archive(rows)
        report = self.fixture.parse()

        self.assertEqual(report["outcome"], "parsed")
        return raw, Path(report["output_dir"])

    def load(self, directory):
        f = self.fixture
        return load_parsed(
            f.source, f.settings, f.root, directory, self.db
        )

    def edit_csv(self, directory, edit):
        report_path = directory / "report.json"
        report = json.loads(report_path.read_text())

        name = (
            "precinct_results.csv"
            if self.fixture.source.format == "zip"
            else "county_demographics.csv"
        )
        path = directory / name

        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)

        edit(rows)

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        report["output_sha256"][name] = file_hash(path)
        report_path.write_text(json.dumps(report), encoding="utf-8")

    def test_load_and_repeat_do_not_duplicate_votes(self):
        _, directory = self.parsed()
        first, second = self.load(directory), self.load(directory)

        self.assertEqual(first["outcome"], "succeeded")
        self.assertEqual(second["outcome"], "skipped")
        self.assertEqual(first["release_id"], second["release_id"])

        count = self.query(
            "SELECT count(*) AS n FROM landing.nc_results"
        )[0]["n"]

        total = self.query(
            "SELECT sum(total_votes) AS n FROM landing.nc_results"
        )[0]["n"]

        self.assertEqual(count, 10)
        self.assertEqual(total, 100)
        self.assertEqual(
            len(self.query("SELECT * FROM ops.load_attempts")), 2
        )

    def test_failing_copy_rolls_back_and_preserves_previous_release(self):
        _, first_dir = self.parsed()
        first = self.load(first_dir)

        rows = [row.copy() for row in self.fixture.rows]
        rows[0][9], rows[0][13] = "1", "9"

        _, second_dir = self.parsed(rows)
        self.edit_csv(
            second_dir,
            lambda records: records[1].update(total_votes="11"),
        )

        failed = self.load(second_dir)

        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM landing.nc_results"
            )[0]["n"],
            10,
        )

        loaded = self.query(
            """
            SELECT release_id
            FROM ops.processing_releases
            WHERE loaded_at IS NOT NULL
            """
        )

        self.assertEqual(
            loaded, [{"release_id": first["release_id"]}]
        )

        attempt = self.query(
            """
            SELECT outcome
            FROM ops.load_attempts
            WHERE load_attempt_id=%s
            """,
            (failed["load_attempt_id"],),
        )[0]

        self.assertEqual(attempt["outcome"], "failed")

    def test_downward_correction_is_a_new_complete_snapshot(self):
        _, directory = self.parsed()
        first = self.load(directory)

        rows = [row.copy() for row in self.fixture.rows]
        rows[0][9], rows[0][13] = "1", "9"

        _, directory = self.parsed(rows)
        second = self.load(directory)

        self.assertEqual(second["outcome"], "succeeded")
        self.assertNotEqual(
            first["release_id"], second["release_id"]
        )

        totals = self.query(
            """
            SELECT sum(total_votes) AS n
            FROM landing.nc_results
            WHERE release_id=%s AND contest_id='NC-SEN'
            """,
            (second["release_id"],),
        )

        self.assertEqual(totals[0]["n"], 19)

    def test_tampered_raw_is_rejected_and_logged(self):
        raw, directory = self.parsed()
        raw.write_bytes(b"changed")

        result = self.load(directory)

        self.assertEqual(result["outcome"], "failed")
        self.assertIn("SHA-256", result["error"])

        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM landing.nc_results"
            )[0]["n"],
            0,
        )

        self.assertEqual(
            self.query(
                "SELECT outcome FROM ops.load_attempts"
            )[0]["outcome"],
            "failed",
        )

    def test_identical_release_identity_cannot_hide_different_output(self):
        _, directory = self.parsed()

        self.assertEqual(
            self.load(directory)["outcome"], "succeeded"
        )

        self.edit_csv(
            directory,
            lambda rows: rows[0].update(
                election_day="1", total_votes="9"
            ),
        )

        self.assertIn(
            "different output", self.load(directory)["error"]
        )

    def test_fetch_logs_are_imported_once_and_reused_on_304(self):
        raw, directory = self.parsed()
        f = self.fixture

        metadata = (
            f.root / "metadata" / f.source.source_id / "2026-10-30"
        )
        metadata.mkdir(parents=True)

        for status, outcome in (
            (200, "downloaded"),
            (304, "unchanged"),
        ):
            identifier = uuid.uuid4().hex

            record = dict(
                schema_version=1,
                fetch_id=identifier,
                run_id=uuid.uuid4().hex,
                source_id=f.source.source_id,
                state="NC",
                election_date="2026-11-03",
                sha256=file_hash(raw),
                started_at=utc_now(),
                finished_at=utc_now(),
                http_status=status,
                outcome=outcome,
            )

            (metadata / (identifier + ".json")).write_text(
                json.dumps(record)
            )

        self.load(directory)
        self.load(directory)

        self.assertEqual(
            len(self.query("SELECT * FROM ops.fetch_attempts")), 2
        )
        self.assertEqual(
            len(self.query("SELECT * FROM ops.snapshots")), 1
        )

    def demographics(self):
        f = self.fixture

        f.source = replace(
            f.source,
            dataset="absentee_early_demographics",
            format="csv",
            expected_members=(),
            expected_columns=(
                "county_name",
                "abs_request_type",
                "party_cd",
                "gender",
                "race",
                "ethnicity",
                "0",
            ),
        )

        path = f.root / "input.csv"
        path.write_text(
            "county_name,abs_request_type,party_cd,gender,race,ethnicity,0\n"
            "WAKE,ONE-STOP,,FEMALE,ASIAN,UNDESIGNATED,7\n"
            "DURHAM,ONE-STOP,,FEMALE,ASIAN,UNDESIGNATED,3\n"
        )

        f.install(path)
        return Path(f.parse()["output_dir"])

    def test_demographics_and_null_values(self):
        result = self.load(self.demographics())

        self.assertEqual(result["outcome"], "succeeded")

        totals = self.query(
            """
            SELECT sum(ballot_count) AS n
            FROM landing.nc_absentee_demographics
            """
        )

        self.assertEqual(totals[0]["n"], 10)

        row = self.query(
            """
            SELECT party_registration
            FROM landing.nc_absentee_demographics
            """
        )[0]

        self.assertIsNone(row["party_registration"])

    def test_demographic_unique_groups_include_nulls(self):
        directory = self.demographics()

        self.edit_csv(
            directory,
            lambda rows: rows[1].update(county="WAKE"),
        )

        self.assertEqual(
            self.load(directory)["outcome"], "failed"
        )

        self.assertEqual(
            self.query(
                """
                SELECT count(*) AS n
                FROM landing.nc_absentee_demographics
                """
            )[0]["n"],
            0,
        )

    def test_concurrent_loads_share_one_release(self):
        _, directory = self.parsed()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(self.load, [directory, directory])
            )

        self.assertEqual(
            sorted(row["outcome"] for row in results),
            ["skipped", "succeeded"],
        )

        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM landing.nc_results"
            )[0]["n"],
            10,
        )