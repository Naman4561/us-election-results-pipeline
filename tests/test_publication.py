"""Artifact checks offline; publication checks in an opt-in *_test database."""

import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_parsing as fixtures

from election_pipeline import publication
from election_pipeline.database import connect, connection_settings, load_parsed
from election_pipeline.publication import checked_results, publish, restore


class PublicationArtifactTests(unittest.TestCase):
    def test_only_a_complete_successful_invocation_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            manifest = {
                "metadata": {"invocation_id": "one"},
                "nodes": {
                    "model.a": {
                        "resource_type": "model",
                        "config": {"materialized": "table"},
                    },
                    "test.a": {
                        "resource_type": "test",
                        "name": "release_completeness",
                    },
                },
                "unit_tests": {"unit_test.a": {}},
            }
            (path / "manifest.json").write_text(json.dumps(manifest))

            good = [
                {"unique_id": "model.a", "status": "success"},
                {"unique_id": "test.a", "status": "pass"},
                {"unique_id": "unit_test.a", "status": "pass"},
            ]

            cases = [
                ("one", good, True),
                ("old", good, False),
                ("one", good[:-1], False),
                *[
                    (
                        "one",
                        [good[0], {**good[1], "status": status}, good[2]],
                        False,
                    )
                    for status in ("fail", "warn", "skipped", "error")
                ],
            ]

            for invocation, rows, accepted in cases:
                with self.subTest(invocation=invocation, rows=rows):
                    (path / "run_results.json").write_text(
                        json.dumps({
                            "metadata": {"invocation_id": invocation},
                            "results": rows,
                        })
                    )
                    if accepted:
                        checked_results(path)
                    else:
                        with self.assertRaises(ValueError):
                            checked_results(path)
    def test_runner_fast_path_requires_the_same_release_transform_and_status(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn

        identifier = uuid.uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "ok": True,
            "publication_id": identifier,
        }

        with (
            patch.object(
                publication, "connect", return_value=conn
            ),
            patch.object(
                publication,
                "release_metadata",
                return_value={"source_id": "nc"},
            ),
            patch.object(
                publication,
                "project_hash",
                return_value="code-hash",
            ),
            patch.object(
                publication, "build_candidate"
            ) as build,
        ):
            result = publish(
                Path(__file__).resolve().parents[1],
                {},
                12,
                "test",
                skip_current=True,
            )

        self.assertEqual(
            result["outcome"], "already_current"
        )
        self.assertEqual(
            result["publication_id"], str(identifier)
        )
        self.assertEqual(
            conn.execute.call_args.args[1],
            ("nc", 12, "code-hash", "test"),
        )
        build.assert_not_called()

@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"),
    "Set ELECTION_TEST_DATABASE to *_test",
)
class PublicationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError(
                "Publication tests require a disposable *_test database"
            )

        cls.root = Path(__file__).resolve().parents[1]
        cls.db = {**connection_settings(), "dbname": name}

        with connect(cls.db) as conn:
            for relation, filename in [
                ("ops.sources", "001_initial.sql"),
                ("serving.publications", "002_publication.sql"),
            ]:
                exists = conn.execute(
                    "SELECT to_regclass(%s) AS t", (relation,)
                ).fetchone()["t"]
                if exists is None:
                    conn.execute(
                        (cls.root / "migrations" / filename).read_text()
                    )

    def setUp(self):
        self.f = fixtures.ParsingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.source = replace(
            self.f.source,
            source_id="publication_" + uuid.uuid4().hex,
        )

        with connect(self.db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")

    def load(self, rows):
        self.f.archive(rows)
        directory = Path(self.f.parse()["output_dir"])
        result = load_parsed(
            self.f.source,
            self.f.settings,
            self.f.root,
            directory,
            self.db,
        )
        self.assertEqual(result["outcome"], "succeeded")
        return result["release_id"]

    def query(self, statement, parameters=()):
        with connect(self.db) as conn:
            return conn.execute(statement, parameters).fetchall()

    def current(self):
        return str(
            self.query(
                "SELECT publication_id FROM serving.current_publications"
            )[0]["publication_id"]
        )

    def test_publication_failure_idempotency_corrections_and_restore(self):
        first_release = self.load(self.f.rows)
        first = publish(self.root, self.db, first_release, "test")
        first_id = first["publication_id"]
        self.assertEqual(self.current(), first_id)

        repeat = publish(self.root, self.db, first_release, "test")
        self.assertEqual(repeat["outcome"], "skipped")
        self.assertEqual(repeat["publication_id"], first_id)
        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM serving.publications"
            )[0]["n"],
            1,
        )

        rows = [row.copy() for row in self.f.rows]
        rows[0][9], rows[0][13] = "1", "9"  # Downward correction.
        second_release = self.load(rows)

        with connect(self.db) as conn:
            conn.execute(
                """
                DELETE FROM landing.nc_results
                WHERE release_id=%s AND source_line=2
                """,
                (second_release,),
            )

        with self.assertRaisesRegex(ValueError, "dbt build failed"):
            publish(self.root, self.db, second_release, "test")

        self.assertEqual(self.current(), first_id)
        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM serving.publications"
            )[0]["n"],
            1,
        )

        # Repair the deliberately damaged fixture in the disposable database.
        with connect(self.db) as conn:
            conn.execute(
                """
                INSERT INTO landing.nc_results
                SELECT %s, source_line, county, precinct, contest_id,
                       contest_name, source_contest_group_id, contest_type,
                       choice, choice_party, vote_for, real_precinct,
                       1, early_voting, absentee_by_mail, provisional, 9
                FROM landing.nc_results
                WHERE release_id=%s AND source_line=2
                """,
                (second_release, first_release),
            )

        second = publish(self.root, self.db, second_release, "test")
        self.assertNotEqual(self.current(), first_id)

        totals = self.query("""
            SELECT publication_id, sum(total_votes) AS n
            FROM serving.nc_contest_candidate_results
            GROUP BY publication_id
        """)
        self.assertEqual(
            {str(r["publication_id"]): r["n"] for r in totals},
            {first_id: 100, second["publication_id"]: 99},
        )

        original = publication.promote

        def fail_after_copy(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Simulated failure after copying outputs")

        with patch.object(
            publication, "promote", side_effect=fail_after_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "after copying"):
                publish(self.root, self.db, second_release, "unofficial")

        self.assertEqual(self.current(), second["publication_id"])
        self.assertEqual(
            self.query(
                "SELECT count(*) AS n FROM serving.publications"
            )[0]["n"],
            2,
        )
        self.assertEqual(
            self.query(
                """
                SELECT count(*) AS n
                FROM serving.nc_contest_candidate_results
                """
            )[0]["n"],
            10,
        )

        restore(self.db, uuid.UUID(first_id))
        self.assertEqual(self.current(), first_id)

        with connect(self.db) as conn:
            conn.execute("SELECT pg_advisory_lock(20261103, 1)")
            with self.assertRaisesRegex(ValueError, "already running"):
                publish(self.root, self.db, second_release, "test")

        with connect(self.db) as conn, conn.transaction():
            conn.execute("SET LOCAL ROLE election_dashboard")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) AS n FROM serving.publications"
                ).fetchone()["n"],
                2,
            )
            self.assertFalse(
                conn.execute("""
                    SELECT has_table_privilege(
                        current_user,
                        'serving.current_publications',
                        'UPDATE'
                    ) AS ok
                """).fetchone()["ok"]
            )
            self.assertFalse(
                conn.execute("""
                    SELECT has_schema_privilege(
                        current_user, 'landing', 'USAGE'
                    ) AS ok
                """).fetchone()["ok"]
            )