"""Artifact checks offline; publication checks in an opt-in *_test database."""

import csv
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_parsing as fixtures
from loading import migrated

from election_pipeline import publication
from election_pipeline.database import connect, load_parsed
from election_pipeline.parsers.nc import CONTESTS
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
                        "tags": ["results"],
                    },
                    "test.a": {
                        "resource_type": "test",
                        "name": "release_completeness",
                        "depends_on": {"nodes": ["model.a"]},
                    },
                    # Mississippi's models are not built for other states.
                    "model.ms": {"resource_type": "model", "tags": ["ms"]},
                },
                "unit_tests": {"unit_test.a": {"depends_on": {"nodes": ["model.a"]}}},
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
                        json.dumps(
                            {
                                "metadata": {"invocation_id": invocation},
                                "results": rows,
                            }
                        )
                    )
                    if accepted:
                        checked_results(path, ["results"])
                    else:
                        with self.assertRaises(ValueError):
                            checked_results(path, ["results"])

    def test_runner_fast_path_requires_the_same_release_transform_and_status(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn

        identifier = uuid.uuid4()
        conn.execute.return_value.fetchone.return_value = {
            "ok": True,
            "publication_id": identifier,
            "release_id": 12,
            "transform_sha256": "code-hash",
            "status": "test",
        }

        with (
            patch.object(publication, "connect", return_value=conn),
            patch.object(
                publication,
                "release_metadata",
                return_value={"source_id": "nc", "state": "NC", "release_id": 12},
            ),
            patch.object(
                publication,
                "project_hash",
                return_value="code-hash",
            ),
            patch.object(publication, "build_candidate") as build,
        ):
            result = publish(
                Path(__file__).resolve().parents[1],
                {},
                12,
                "test",
                skip_current=True,
            )

        self.assertEqual(result["outcome"], "already_current")
        self.assertEqual(result["publication_id"], str(identifier))
        self.assertEqual(
            conn.execute.call_args.args[1],
            ("nc",),
        )
        build.assert_not_called()

    def test_an_official_publication_needs_the_states_own_official_flag(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        for report in ({"official_results": False}, {}, {"official_results": "yes"}):
            with (
                self.subTest(report=report),
                patch.object(publication, "connect", return_value=conn),
                patch.object(
                    publication,
                    "release_metadata",
                    return_value={"source_id": "ga", "state": "GA", "parser_report": report},
                ),
                patch.object(publication, "build_candidate") as build,
                self.assertRaisesRegex(ValueError, "does not mark this snapshot as official"),
            ):
                publication.prepare(Path(__file__).resolve().parents[1], {}, 12, "official")
            build.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Status must be"):
            publication.prepare(Path(__file__).resolve().parents[1], {}, 12, "certified")

    def test_equivalent_iowa_archive_can_reuse_the_current_publication(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        publication_id = uuid.uuid4()
        metadata = {"source_id": publication.IOWA_SOURCE, "state": "IA", "release_id": 7479}
        current = {
            "publication_id": publication_id,
            "release_id": 7640,
            "transform_sha256": "code-hash",
            "status": "test",
        }
        with (
            patch.object(publication, "connect", return_value=conn),
            patch.object(publication, "publication_lock"),
            patch.object(publication, "release_metadata", return_value=metadata),
            patch.object(publication, "current_publication", return_value=current),
            patch.object(publication, "project_hash", return_value="code-hash"),
            patch.object(publication, "equivalent_iowa_release", return_value=True) as equivalent,
            patch.object(publication, "same_results") as same,
            patch.object(publication, "build_candidate") as build,
        ):
            root = Path(__file__).resolve().parents[1]
            result = publish(root, {}, 7479, "test", skip_current=True)
            self.assertEqual(
                result,
                {
                    "outcome": "already_current",
                    "publication_id": str(publication_id),
                    "release_id": 7640,
                },
            )
            with self.assertRaisesRegex(ValueError, "earlier release"):
                publish(root, {}, 7479, "unofficial", skip_current=True)
            equivalent.assert_called_once_with(conn, current, metadata)
            equivalent.return_value = False
            with self.assertRaisesRegex(ValueError, "earlier release"):
                publish(root, {}, 7479, "test", skip_current=True)
        build.assert_not_called()
        same.assert_not_called()

    def test_earlier_release_with_the_live_outputs_and_update_time_is_not_a_rollback(self):
        live = {
            "output_sha256": {"results.csv": "a" * 64},
            "output_rows": 180,
            "contests": ["WA-01"],
            "minimum_candidates": 2,
            "source_update_text": "2026-11-08T01:04:18.1236473Z",
        }
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"parser_report": live}
        current = {"snapshot_id": 2203555, "release_id": 10047}
        incoming = {
            "source_id": "wa_2026_general",
            "snapshot_id": 2133791,
            "release_id": 9807,
            "parser_report": dict(live),
        }
        publication.require_forward_release(conn, current, incoming)
        self.assertEqual(conn.execute.call_args.args[1], (10047,))
        for key, value in (
            ("source_update_text", "2026-11-08T00:04:18Z"),
            ("output_sha256", {"results.csv": "b" * 64}),
            ("output_rows", 179),
            ("source_update_text", None),
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "earlier release"),
            ):
                publication.require_forward_release(
                    conn, current, {**incoming, "parser_report": {**live, key: value}}
                )
        # Iowa and Florida keep their own, stricter equivalence rules.
        with self.assertRaisesRegex(ValueError, "earlier release"):
            publication.require_forward_release(
                conn, current, {**incoming, "source_id": publication.IOWA_SOURCE}
            )

    def test_florida_orders_distinct_snapshots_by_separated_file_time_ranges(self):
        earlier = datetime(2026, 11, 4, 15, 0, 3, tzinfo=timezone.utc)
        later_start = datetime(2026, 11, 4, 21, 48, 3, tzinfo=timezone.utc)
        later_end = datetime(2026, 11, 5, 15, 48, 3, tzinfo=timezone.utc)
        observed = datetime(2026, 11, 6, tzinfo=timezone.utc)
        conn = MagicMock()
        cursor = conn.execute.return_value
        earlier_headers = [
            {"modified": stamp, "first_seen": observed}
            for stamp in ("Wed, 04 Nov 2026 14:33:03 GMT", "Wed, 04 Nov 2026 15:00:03 GMT")
        ]
        cursor.fetchall.return_value = earlier_headers
        current = {"snapshot_id": 959576, "release_id": 5419, "source_modified_at": None}
        incoming = {
            "source_id": publication.FLORIDA_SOURCE,
            "snapshot_id": 931656,
            "release_id": 5317,
            "source_modified_range": (later_start, later_end),
            "source_url": "https://example.org/results.txt",
        }
        publication.require_forward_release(conn, current, incoming)
        self.assertEqual(
            conn.execute.call_args.args[1],
            (publication.FLORIDA_SOURCE, 959576, incoming["source_url"]),
        )

        cursor.fetchall.return_value = [
            {"modified": stamp, "first_seen": observed}
            for stamp in ("Wed, 04 Nov 2026 21:48:03 GMT", "Thu, 05 Nov 2026 15:48:03 GMT")
        ]
        with self.assertRaisesRegex(ValueError, "earlier Florida source file"):
            publication.require_forward_release(
                conn,
                {"snapshot_id": 931656, "release_id": 5317, "source_modified_at": later_end},
                {
                    **incoming,
                    "snapshot_id": 959576,
                    "release_id": 5500,
                    "source_modified_range": (earlier, earlier),
                },
            )

        cursor.fetchall.return_value = earlier_headers
        for ambiguous in (None, (earlier, later_end)):
            with (
                self.subTest(ambiguous=ambiguous),
                self.assertRaisesRegex(ValueError, "earlier release"),
            ):
                publication.require_forward_release(
                    conn,
                    current,
                    {**incoming, "source_modified_range": ambiguous, "release_id": 5500},
                )

    def test_florida_rejects_older_file_before_building_candidate(self):
        earlier = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
        later = datetime(2026, 11, 5, 15, 48, tzinfo=timezone.utc)
        conn = MagicMock()
        conn.__enter__.return_value = conn
        current = {
            "snapshot_id": 1,
            "release_id": 12,
            "source_modified_at": later,
            "transform_sha256": "code-hash",
            "status": "test",
        }
        incoming = {
            "source_id": publication.FLORIDA_SOURCE,
            "state": "FL",
            "snapshot_id": 2,
            "release_id": 13,
            "source_modified_range": (earlier, earlier),
            "source_url": "https://example.org/results.txt",
        }
        with (
            patch.object(publication, "connect", return_value=conn),
            patch.object(publication, "release_metadata", return_value=incoming),
            patch.object(publication, "current_publication", return_value=current),
            patch.object(publication, "florida_file_modified_range", return_value=(later, later)),
            patch.object(publication, "project_hash", return_value="code-hash"),
            patch.object(publication, "same_results") as same,
            patch.object(publication, "build_candidate") as build,
        ):
            with self.assertRaisesRegex(ValueError, "earlier Florida source file"):
                publish(Path(__file__).resolve().parents[1], {}, 13, "test", skip_current=True)
        build.assert_not_called()
        same.assert_not_called()

    def test_florida_file_time_range_requires_valid_past_headers(self):
        conn = MagicMock()
        cursor = conn.execute.return_value
        observed = datetime(2026, 11, 6, tzinfo=timezone.utc)
        valid = {"modified": "Thu, 05 Nov 2026 15:48:03 GMT", "first_seen": observed}
        for rows in (
            [],
            [{**valid, "modified": "invalid"}],
            [{**valid, "modified": "Fri, 06 Nov 2026 01:00:00 GMT"}],
        ):
            with self.subTest(rows=rows):
                cursor.fetchall.return_value = rows
                self.assertIsNone(publication.florida_file_modified_range(conn, 42, "source"))


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"),
    "Set ELECTION_TEST_DATABASE to *_test",
)
class PublicationDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = migrated()

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
            self.query("SELECT publication_id FROM serving.current_publications")[0][
                "publication_id"
            ]
        )

    def test_identical_results_from_a_new_snapshot_are_not_republished(self):
        first_release = self.load(self.f.rows)
        first = publish(self.root, self.db, first_release, "test", skip_current=True)

        # Same rows, different raw bytes (as when a file embeds its download time).
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t")
        writer.writerow(fixtures.RESULT_HEADER)
        writer.writerows(self.f.rows)
        path = self.f.root / "input.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("results.txt", output.getvalue())
        self.f.install(path)
        directory = Path(self.f.parse()["output_dir"])
        second_release = load_parsed(
            self.f.source, self.f.settings, self.f.root, directory, self.db
        )["release_id"]
        self.assertNotEqual(first_release, second_release)

        repeat = publish(self.root, self.db, second_release, "test", skip_current=True)
        self.assertEqual(repeat["outcome"], "already_current")
        self.assertEqual(repeat["publication_id"], first["publication_id"])
        self.assertEqual(repeat["identical_to_release"], first_release)
        self.assertEqual(self.query("SELECT count(*) AS n FROM serving.publications")[0]["n"], 1)

        # A real change publishes normally.
        rows = [row.copy() for row in self.f.rows]
        rows[0][9], rows[0][13] = "1", "9"
        changed = publish(self.root, self.db, self.load(rows), "test", skip_current=True)
        self.assertNotEqual(changed["publication_id"], first["publication_id"])

    def test_publication_failure_idempotency_corrections_and_restore(self):
        first_release = self.load(self.f.rows)
        first = publish(self.root, self.db, first_release, "test")
        first_id = first["publication_id"]
        self.assertEqual(self.current(), first_id)

        repeat = publish(self.root, self.db, first_release, "test")
        self.assertEqual(repeat["outcome"], "skipped")
        self.assertEqual(repeat["publication_id"], first_id)
        self.assertEqual(
            self.query("SELECT count(*) AS n FROM serving.publications")[0]["n"],
            1,
        )

        rows = [row.copy() for row in self.f.rows]
        rows[0][9], rows[0][13] = "1", "9"  # Downward correction.
        second_release = self.load(rows)

        with connect(self.db) as conn:
            # Take one precinct row (and its vote methods) out of the loaded release.
            for table in ("result_methods", "results"):
                conn.execute(
                    f"DELETE FROM landing.{table} WHERE release_id=%s AND source_line=2",
                    (second_release,),
                )

        with self.assertRaisesRegex(ValueError, "dbt build failed"):
            publish(self.root, self.db, second_release, "test")

        self.assertEqual(self.current(), first_id)
        self.assertEqual(
            self.query("SELECT count(*) AS n FROM serving.publications")[0]["n"],
            1,
        )

        # Repair the deliberately damaged fixture in the disposable database.
        with connect(self.db) as conn:
            conn.execute(
                "INSERT INTO landing.results SELECT %s, source_line, state, unit_type, county,"
                " precinct, contest_id, source_contest_id, source_candidate_id, choice,"
                " choice_party, 9, precincts_reporting, precincts_total, attributes"
                " FROM landing.results WHERE release_id=%s AND source_line=2",
                (second_release, first_release),
            )
            conn.execute(
                "INSERT INTO landing.result_methods SELECT %s, source_line, method,"
                " CASE method WHEN 'election_day' THEN 1 ELSE votes END"
                " FROM landing.result_methods WHERE release_id=%s AND source_line=2",
                (second_release, first_release),
            )

        second = publish(self.root, self.db, second_release, "test")
        self.assertNotEqual(self.current(), first_id)

        totals = self.query("""
            SELECT publication_id, sum(total_votes) AS n
            FROM serving.contest_results
            GROUP BY publication_id
        """)
        self.assertEqual(
            {str(r["publication_id"]): r["n"] for r in totals},
            # Two precincts of 10 votes in each NC race; the correction removes one vote.
            {first_id: 20 * len(CONTESTS), second["publication_id"]: 20 * len(CONTESTS) - 1},
        )

        original = publication.promote

        def fail_after_copy(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Simulated failure after copying outputs")

        with patch.object(publication, "promote", side_effect=fail_after_copy):
            with self.assertRaisesRegex(RuntimeError, "after copying"):
                publish(self.root, self.db, second_release, "unofficial")

        self.assertEqual(self.current(), second["publication_id"])
        self.assertEqual(
            self.query("SELECT count(*) AS n FROM serving.publications")[0]["n"],
            2,
        )
        self.assertEqual(
            self.query(
                """
                SELECT count(*) AS n
                FROM serving.contest_results
                """
            )[0]["n"],
            # One candidate per race in each of the two publications.
            2 * len(CONTESTS),
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
                conn.execute("SELECT count(*) AS n FROM serving.publications").fetchone()["n"],
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
