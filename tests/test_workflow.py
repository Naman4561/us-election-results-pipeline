"""Runner recovery tests and an opt-in PostgreSQL/dbt rehearsal."""

import csv
import json
import os
import threading
import unittest
import uuid
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import test_parsing as fixtures
from loading import migrated
from test_collection import Handler

from election_pipeline import database, publication, workflow
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, file_hash


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ParsingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

        self.raw = self.f.archive()
        self.sha = file_hash(self.raw)
        self.db = {"password": "sensitive-password"}

        self.collect = self.enterContext(
            patch.object(
                workflow,
                "collect_source",
                return_value={
                    "outcome": "downloaded",
                    "sha256": self.sha,
                },
            )
        )

        self.enterContext(
            patch.object(
                database,
                "connection_settings",
                return_value=self.db,
            )
        )

        self.load = self.enterContext(
            patch.object(
                database,
                "load_parsed",
                return_value={
                    "outcome": "succeeded",
                    "release_id": 7,
                },
            )
        )

        self.publish = self.enterContext(
            patch.object(
                publication,
                "publish",
                return_value={
                    "outcome": "published",
                    "publication_id": "frozen-7",
                    "release_id": 7,
                },
            )
        )

    def run_source(self, **kwargs):
        return workflow.run_source(
            self.f.source,
            self.f.settings,
            self.f.root,
            root=Path(__file__).resolve().parents[1],
            status="test",
            **kwargs,
        )

    def test_one_pinned_snapshot_and_stage_timings(self):
        result = self.run_source()

        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["raw_sha256"], self.sha)
        self.assertEqual(
            [stage["stage"] for stage in result["stages"]],
            ["collect", "parse", "load", "validate_and_publish"],
        )
        self.assertTrue(all(stage["duration_seconds"] >= 0 for stage in result["stages"]))
        self.assertEqual(
            json.loads(Path(result["report_path"]).read_text()),
            result,
        )
        self.assertTrue(self.publish.call_args.kwargs["skip_current"])
        self.assertEqual(self.publish.call_args.args[2], 7)

    def test_unchanged_snapshot_recovers_failed_load_then_failed_publication(self):
        self.load.side_effect = [
            {"outcome": "failed", "error": "DB unavailable"},
            {"outcome": "succeeded", "release_id": 7},
            {"outcome": "skipped", "release_id": 7},
        ]

        first = self.run_source()
        self.assertEqual(first["outcome"], "failed")
        self.publish.assert_not_called()

        self.collect.return_value = {
            "outcome": "unchanged",
            "sha256": self.sha,
        }
        self.publish.side_effect = [
            RuntimeError("dbt failed"),
            {
                "outcome": "published",
                "publication_id": "frozen-7",
            },
        ]

        self.assertEqual(self.run_source()["outcome"], "failed")
        self.assertEqual(self.run_source()["outcome"], "succeeded")
        self.assertEqual(self.load.call_count, 3)

    def test_not_due_and_deferred_can_resume_cached_downstream_work_without_http(self):
        store = ArchiveStore(self.f.root, self.f.source)
        state = store.read_state()

        store.write_state(
            {
                **state,
                "source_signature": self.f.source.signature(self.f.settings),
            }
        )

        for outcome in ("not_due", "deferred"):
            self.collect.return_value = {"outcome": outcome}
            self.assertEqual(self.run_source()["outcome"], "succeeded")

    def test_missing_or_changed_cached_identity_cannot_resume_not_due(self):
        self.collect.return_value = {"outcome": "not_due"}

        self.assertEqual(self.run_source()["outcome"], "not_due")
        self.load.assert_not_called()

    def test_invalid_input_and_reference_mismatch_block_publication(self):
        self.raw.write_bytes(b"corrupt")

        self.assertEqual(self.run_source()["outcome"], "failed")
        self.load.assert_not_called()

        self.sha = file_hash(self.f.archive())
        self.collect.return_value = {
            "outcome": "downloaded",
            "sha256": self.sha,
        }

        reference = self.f.root / "wrong.csv"

        with reference.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("election_date", "contest_id", "choice", "total_votes"))
            writer.writerow(("2026-11-03", "NC-SEN", "Candidate A", 19))

        result = self.run_source(reference=reference)

        self.assertIn("reference mismatch", result["error"])
        self.load.assert_not_called()
        self.publish.assert_not_called()

    def test_bad_fetch_never_loads_previous_good_snapshot(self):
        self.collect.return_value = {"outcome": "invalid_archive"}

        self.assertEqual(self.run_source()["outcome"], "failed")
        self.load.assert_not_called()

    def test_replay_makes_no_http_request_or_implicit_publication(self):
        result = self.run_source(replay_sha256=self.sha)

        self.assertEqual(result["outcome"], "replayed_not_published")
        self.collect.assert_not_called()
        self.load.assert_called_once()
        self.publish.assert_not_called()

        result = self.run_source(
            replay_sha256=self.sha,
            publish_replay=True,
        )

        self.assertEqual(result["outcome"], "succeeded")
        self.publish.assert_called_once()

    def test_prepare_only_validates_live_and_archived_inputs_without_promoting(self):
        with patch.object(
            publication,
            "prepare",
            return_value={"outcome": "validated", "candidate_id": "candidate-7", "release_id": 7},
        ) as prepare:
            for replay in (None, self.sha):
                result = self.run_source(prepare_only=True, replay_sha256=replay)
                self.assertEqual(result["outcome"], "validated")
                self.assertEqual(result["candidate_id"], "candidate-7")
                self.assertEqual(result["stages"][-1]["stage"], "validate_candidate")
                self.assertNotIn("publication_id", result)
            self.assertEqual(prepare.call_count, 2)
            self.publish.assert_not_called()
        with self.assertRaisesRegex(ValueError, "cannot publish"):
            self.run_source(prepare_only=True, replay_sha256=self.sha, publish_replay=True)

    def test_check_only_replays_the_archived_snapshot_and_leaves_collector_state(self):
        store = ArchiveStore(self.f.root, self.f.source)
        # A new release may sign the source differently; a check must not reset its state.
        store.write_state({**store.read_state(), "sha256": self.sha, "source_signature": "old"})
        before = store.state_path.read_bytes()
        with (
            patch.object(
                publication,
                "prepare",
                return_value={"outcome": "validated", "candidate_id": "c", "release_id": 7},
            ),
            patch.object(publication, "discard", return_value={"outcome": "discarded"}) as discard,
        ):
            result = self.run_source(check_only=True)
        self.assertEqual(result["outcome"], "validated")
        self.assertEqual(result["raw_sha256"], self.sha)
        self.collect.assert_not_called()
        discard.assert_called_once()
        self.publish.assert_not_called()
        self.assertEqual(store.state_path.read_bytes(), before)
        self.assertFalse((self.f.root / "runs" / self.f.source.source_id / "latest.json").exists())

        # A snapshot of the address the source used before it moved is not checked.
        store.write_state({**json.loads(before), "url": "https://example.org/old-feed"})
        result = self.run_source(check_only=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["error"], workflow.NO_SNAPSHOT)

        store.write_state({"source_signature": "old"})
        result = self.run_source(check_only=True)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("No archived snapshot", result["error"])
        self.collect.assert_not_called()

    def test_full_run_lock_rejects_overlap_without_overwriting_latest_report(self):
        first = self.run_source()
        latest = Path(first["report_path"]).parent / "latest.json"

        with ArchiveStore(self.f.root, self.f.source).lock(stage="pipeline"):
            result = self.run_source()

        self.assertEqual(result["outcome"], "locked")
        self.assertEqual(
            json.loads(latest.read_text())["run_id"],
            first["run_id"],
        )
        self.assertEqual(self.collect.call_count, 1)

    def test_failure_redacts_password_and_releases_lock(self):
        self.publish.side_effect = RuntimeError("could not connect: sensitive-password")

        result = self.run_source()

        self.assertNotIn(
            self.db["password"],
            Path(result["report_path"]).read_text(),
        )

        with ArchiveStore(self.f.root, self.f.source).lock(stage="pipeline"):
            pass

    def test_process_interruption_is_recorded_and_next_run_can_recover(self):
        self.load.side_effect = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_source()

        latest = self.f.root / "runs" / self.f.source.source_id / "latest.json"

        self.assertEqual(
            json.loads(latest.read_text())["outcome"],
            "interrupted",
        )

        self.load.side_effect = None
        self.assertEqual(self.run_source()["outcome"], "succeeded")


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"),
    "Requires a disposable *_test PostgreSQL DB",
)
class IntegratedWorkflowTests(unittest.TestCase):
    def test_http_to_dashboard_retry_correction_and_replay(self):
        name = os.environ["ELECTION_TEST_DATABASE"]

        if not name.endswith("_test"):
            raise ValueError("Integration requires a disposable *_test database")

        db = {
            **database.connection_settings(),
            "dbname": name,
        }
        root = Path(__file__).resolve().parents[1]

        db = migrated()
        with database.connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")

        f = fixtures.ParsingTests()
        f.setUp()
        self.addCleanup(f.doCleanups)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        f.source = replace(
            f.source,
            source_id="workflow_" + uuid.uuid4().hex,
            url=(f"http://127.0.0.1:{server.server_port}/results.zip"),
        )

        Handler.responses = []
        Handler.requests = []

        def run(status="test"):
            with patch.object(
                database,
                "connection_settings",
                return_value=db,
            ):
                return workflow.run_source(
                    f.source,
                    f.settings,
                    f.root,
                    root=root,
                    status=status,
                )

        def current():
            with database.connect(db) as conn:
                row = conn.execute(
                    """
                    SELECT publication_id
                    FROM serving.current_publications
                    """
                ).fetchone()
                return str(row["publication_id"])

        Handler.responses = [
            {
                "body": f.archive().read_bytes(),
                "headers": {"ETag": '"v1"'},
            }
        ]

        first = run()
        self.assertEqual(first["outcome"], "succeeded", first)

        Handler.responses = [{"status": 304}]

        with patch.object(
            publication,
            "build_candidate",
            wraps=publication.build_candidate,
        ) as build:
            repeat = run()
            self.assertEqual(repeat["outcome"], "already_current", repeat)
            build.assert_not_called()

        self.assertEqual(
            repeat["publication_id"],
            first["publication_id"],
        )

        rows = [row.copy() for row in f.rows]
        rows[0][9], rows[0][13] = "1", "9"

        Handler.responses = [
            {
                "body": f.archive(rows).read_bytes(),
                "headers": {"ETag": '"v2"'},
            }
        ]

        with patch.object(
            publication,
            "build_candidate",
            side_effect=RuntimeError("injected dbt failure"),
        ):
            self.assertEqual(run()["outcome"], "failed")

        self.assertEqual(current(), first["publication_id"])

        Handler.responses = [{"status": 304}]
        corrected = run()

        self.assertEqual(corrected["outcome"], "succeeded", corrected)
        self.assertNotEqual(
            corrected["publication_id"],
            first["publication_id"],
        )

        from election_pipeline.dashboard.server import STATIC, make_payload, published_rows

        with database.connect(db) as conn:
            params = (corrected["publication_id"],)

            methods = state_adapter("NC").methods[1:]
            county = published_rows(conn, params[0], "county_results", ("county",), methods)
            contest = published_rows(conn, params[0], "contest_results", (), methods)

            totals = conn.execute(
                """
                SELECT publication_id, sum(total_votes) AS n
                FROM serving.contest_results
                GROUP BY publication_id
                """
            ).fetchall()

            self.assertEqual(
                {str(row["publication_id"]): row["n"] for row in totals},
                # Two precincts of 10 votes in each race; the correction removes one vote.
                {
                    first["publication_id"]: 20 * len(state_adapter("NC").races),
                    corrected["publication_id"]: 20 * len(state_adapter("NC").races) - 1,
                },
            )

            count = conn.execute("SELECT count(*) AS n FROM landing.results").fetchone()["n"]

            # Both releases keep every precinct row: two per race.
            self.assertEqual(count, 2 * 2 * len(state_adapter("NC").races))

        payload = make_payload(
            json.loads((STATIC / "nc-map.json").read_text()),
            {"publication_id": corrected["publication_id"]},
            county,
            contest,
        )

        self.assertEqual(
            payload["races"]["NC-SEN"]["stats"]["total_votes"]["total"],
            19,
        )

        requests = len(Handler.requests)

        with patch.object(
            database,
            "connection_settings",
            return_value=db,
        ):
            replay = workflow.run_source(
                f.source,
                f.settings,
                f.root,
                root=root,
                status="test",
                replay_sha256=first["raw_sha256"],
            )

        self.assertEqual(replay["outcome"], "replayed_not_published", replay)
        self.assertEqual(len(Handler.requests), requests)
        self.assertEqual(current(), corrected["publication_id"])

        original_publish = publication.publish

        def lose_acknowledgement(*args, **kwargs):
            original_publish(*args, **kwargs)
            raise OSError("injected lost acknowledgement after commit")

        Handler.responses = [{"status": 304}]

        with patch.object(
            publication,
            "publish",
            side_effect=lose_acknowledgement,
        ):
            self.assertEqual(
                run(status="unofficial")["outcome"],
                "failed",
            )

        committed = current()
        self.assertNotEqual(committed, corrected["publication_id"])

        Handler.responses = [{"status": 304}]

        with patch.object(publication, "build_candidate") as build:
            resumed = run(status="unofficial")

        self.assertEqual(resumed["outcome"], "already_current", resumed)
        self.assertEqual(resumed["publication_id"], committed)
        build.assert_not_called()

        requests = len(Handler.requests)

        with patch.object(
            database,
            "connection_settings",
            return_value=db,
        ):
            complete_replay = workflow.run_source(
                f.source,
                f.settings,
                f.root,
                root=root,
                status="test",
                replay_sha256=corrected["raw_sha256"],
                publish_replay=True,
            )

        self.assertEqual(
            complete_replay["outcome"],
            "succeeded",
            complete_replay,
        )
        self.assertEqual(current(), corrected["publication_id"])
        self.assertEqual(len(Handler.requests), requests)
