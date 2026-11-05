"""Shared contracts, independent state jobs, national API and opt-in PostgreSQL checks."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from election_pipeline import publication
from election_pipeline.dashboard.server import STATIC, create_app, demo_payload, national_payload
from election_pipeline.database import connect, connection_settings, load_parsed
from election_pipeline.states import publication_lock, state_adapter


class MultiStateTests(unittest.TestCase):
    def test_lock_keys_are_distinct_and_share_the_backup_barrier(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"ok": True}
        publication_lock(conn, "NC")
        publication_lock(conn, "KY")
        calls = conn.execute.call_args_list
        self.assertIn("lock_shared", calls[0].args[0])
        self.assertIn("lock_shared", calls[2].args[0])
        self.assertNotEqual(calls[1].args[1], calls[3].args[1])

    def test_dbt_processes_have_state_selectors_and_private_artifact_paths(self):
        root = Path(__file__).resolve().parents[1]
        db = {
            "host": "127.0.0.1",
            "port": 5432,
            "dbname": "test",
            "user": "writer",
            "password": "secret",
        }
        before = dict(os.environ)
        with (
            patch.object(publication.subprocess, "run") as run,
            patch.object(publication, "checked_results"),
        ):
            run.return_value.returncode = 0
            for code in ("NC", "KY"):
                publication.build_candidate(
                    root,
                    db,
                    {
                        "state": code,
                        "dataset": "election_results",
                        "release_id": 1,
                        "election_date": "2026-11-03",
                        "source_id": code.lower(),
                    },
                    "p_" + code.lower(),
                    root / "data/publications" / code,
                )
            calls = run.call_args_list
            self.assertIn("tag:state_nc", calls[0].args[0])
            self.assertIn("tag:state_ky", calls[1].args[0])
            self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.assertEqual(dict(os.environ), before)

    def test_national_api_and_party_categories(self):
        client = TestClient(create_app(demo=True))
        response = client.get("/api/national")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["calendar"]["senate_states"]), 35)
        self.assertEqual(len(payload["calendar"]["governor_states"]), 36)
        self.assertEqual(
            set(payload["states"]), {"NC", "KY", "MN", "NE", "TX", "GA", "SC", "KS", "OH", "AK"}
        )
        self.assertEqual(len(payload["coverage"]["KY"]), 7)
        self.assertNotIn("early_voting", payload["states"]["KY"]["methods"])
        self.assertEqual(client.get("/api/states/ME").status_code, 404)
        self.assertEqual(client.get("/api/states/KY").status_code, 200)
        cached = client.get("/api/national", headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(len(json.loads((STATIC / "us-map.json").read_text())["districts"]), 435)

    def test_one_unavailable_state_does_not_blank_other_states(self):
        def nc():
            return demo_payload(json.loads((STATIC / "nc-map.json").read_text()))

        def failed():
            raise RuntimeError("database unavailable")

        payload = national_payload({"NC": nc, "KY": failed})
        self.assertEqual(payload["states"]["NC"]["availability"], "available")
        self.assertEqual(payload["states"]["KY"]["availability"], "unavailable")
        self.assertEqual(payload["states"]["KY"]["races"], {})

    def test_fingerprints_block_changed_validated_candidate(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = {
            "release_id": 2,
            "outcome": "running",
            "validated_at": "now",
            "transform_sha256": "same",
            "candidate_schema": "p_ky_test",
            "candidate_fingerprints": {"table": {"sha256": "original"}},
        }
        with (
            patch.object(publication, "connect", return_value=conn),
            patch.object(publication, "release_metadata", return_value={"state": "KY"}),
            patch.object(publication, "publication_lock"),
            patch.object(publication, "project_hash", return_value="same"),
            patch.object(
                publication, "candidate_fingerprints", return_value={"table": {"sha256": "changed"}}
            ),
            patch.object(publication, "promote") as promote,
        ):
            with self.assertRaisesRegex(ValueError, "changed after validation"):
                publication.publish_prepared(Path("."), {}, "candidate")
            promote.assert_not_called()


@unittest.skipUnless(os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable PostgreSQL *_test")
class MultiStateDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = {**connection_settings(), "dbname": name}
        with connect(cls.db) as conn:
            for relation, filename in [
                ("ops.sources", "001_initial.sql"),
                ("serving.publications", "002_publication.sql"),
                ("ky.results", "003_state_schemas.sql"),
                ("tx.results", "004_mn_ne_tx.sql"),
            ]:
                if conn.execute("SELECT to_regclass(%s) AS t", (relation,)).fetchone()["t"] is None:
                    conn.execute((cls.root / "migrations" / filename).read_text())

    def test_real_state_lock_concurrency_and_exclusive_backup(self):
        with connect(self.db) as nc, connect(self.db) as ky, connect(self.db) as other:
            publication_lock(nc, "NC")
            publication_lock(ky, "KY")
            with self.assertRaisesRegex(ValueError, "Another NC"):
                publication_lock(other, "NC")
            self.assertFalse(
                other.execute("SELECT pg_try_advisory_lock(20261103,1) AS ok").fetchone()["ok"]
            )
        with connect(self.db) as backup, connect(self.db) as candidate:
            backup.execute("SELECT pg_advisory_lock(20261103,1)")
            with self.assertRaisesRegex(ValueError, "Backup"):
                publication_lock(candidate, "KY")

    def test_both_states_publish_concurrently_and_reader_cannot_read_ingestion(self):
        import test_kentucky
        import test_parsing

        fixtures = [test_parsing.ParsingTests(), test_kentucky.KentuckyTests()]
        for f in fixtures:
            f.setUp()
            self.addCleanup(f.doCleanups)
        nc, ky = fixtures
        nc.archive(nc.rows)
        parsed = [nc.parse(), ky.parse()]
        with connect(self.db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        releases = []
        for f, report in zip(fixtures, parsed):
            result = load_parsed(f.source, f.settings, f.root, Path(report["output_dir"]), self.db)
            self.assertEqual(result["outcome"], "succeeded", result)
            releases.append(result["release_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(publication.publish, self.root, self.db, r, "test") for r in releases
            ]
            results = [job.result() for job in jobs]
        self.assertEqual([r["outcome"] for r in results], ["published", "published"])
        with connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) AS n FROM serving.current_publications").fetchone()[
                    "n"
                ],
                2,
            )
            conn.execute("SET ROLE election_dashboard")
            for code in ("NC", "KY"):
                schema = state_adapter(code).schema
                self.assertFalse(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (schema + ".results",),
                    ).fetchone()["ok"]
                )
                self.assertTrue(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (schema + ".county_candidate_results",),
                    ).fetchone()["ok"]
                )
