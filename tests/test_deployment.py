"""Expansion ownership, activation gates, failure isolation and live routing rollback."""

import copy
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from psycopg import sql

from election_pipeline import recovery
from election_pipeline.dashboard.server import create_app
from election_pipeline.database import connect, connection_settings
from election_pipeline.deployment import catalog, containers, control, dashboard, migrations
from election_pipeline.settings import CollectorSettings, Source

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64
REVISION = "b" * 40


def fixture(root):
    sources = {}
    for code, worker in (("NC", "baseline"), ("KY", "baseline"), ("GA", "expansion")):
        source = Source(
            code.lower() + "_2026_general", code, "2026-11-03", "https://example.com/a.zip"
        )
        entry = catalog.entry(
            source,
            CollectorSettings(),
            image_id=IMAGE,
            revision=REVISION,
            release_path=root,
            worker=worker,
            status="test",
        )
        entry["races"] = [code + "-SEN"]
        sources[source.source_id] = entry
    return {
        "version": 1,
        "generation": 0,
        "baseline": {
            "image_id": IMAGE,
            "revision": REVISION,
            "source_ids": ["nc_2026_general", "ky_2026_general"],
        },
        "sources": sources,
        "dashboard": {"name": "dashboard", "url": "http://old", "states": ["NC", "KY"]},
    }


def ddl(schema="ga"):
    return f"""BEGIN;
    CREATE SCHEMA {schema};
    REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;
    CREATE TABLE {schema}.results (
        release_id bigint REFERENCES ops.processing_releases,
        choice text CHECK (btrim(choice) <> ''), total_votes bigint CHECK (total_votes >= 0)
    );
    CREATE TABLE {schema}.contest_candidate_results (
        publication_id uuid REFERENCES serving.publications,
        contest_id text, choice text, total_votes bigint
    );
    CREATE INDEX ga_results_release ON {schema}.results (release_id);
    GRANT USAGE ON SCHEMA {schema} TO election_dashboard;
    GRANT SELECT ON {schema}.contest_candidate_results TO election_dashboard;
    COMMIT;"""


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        self.value = fixture(self.root)
        catalog.write(self.path, self.value)
        self.prod = {
            "IMAGE_TAG": REVISION,
            "PIPELINE_SOURCES": "nc_2026_general,ky_2026_general",
            "DATA_DIR": str(self.root / "data"),
            "POSTGRES_DB": "elections",
            "POSTGRES_USER": "admin",
            "POSTGRES_PASSWORD": "administrator-secret",
            "PIPELINE_DB_USER": "writer",
            "PIPELINE_DB_PASSWORD": "writer-secret",
            "DASHBOARD_DB_USER": "reader",
            "DASHBOARD_DB_PASSWORD": "reader-secret",
        }
        self.config = {
            "EXPANSION_CPUS": "0.5",
            "EXPANSION_MEMORY": "768m",
            "CONTROL_STATE_DIR": str(self.root),
            "CADDYFILE": str(self.root / "Caddyfile"),
            "CADDY_CONTAINER": "caddy-id",
            "CONTROL_ROOT": str(self.root),
            "CATALOG_FILE": str(self.path),
        }

    def test_round_trip_keeps_complete_contract_and_pending_state_private(self):
        value = catalog.read(self.path)
        self.assertEqual(catalog.public_states(value), {"NC", "KY"})
        self.assertEqual(catalog.sources(value)["ga_2026_general"].state, "GA")
        self.assertIn("ga", catalog.schemas(value))
        self.assertNotIn("secret", self.path.read_text())
        self.assertEqual(
            asdict(catalog.sources(value)["nc_2026_general"]),
            asdict(catalog.source_from_json(self.value["sources"]["nc_2026_general"]["source"])),
        )

    def test_rejects_shared_state_owner_mutable_image_and_baseline_drift(self):
        for change in (
            lambda v: v["sources"]["ga_2026_general"].update(image_id="latest"),
            lambda v: v["sources"]["ga_2026_general"].update(public=True),
            lambda v: v["baseline"].update(source_ids=[]),
        ):
            value = copy.deepcopy(self.value)
            change(value)
            with self.assertRaises(ValueError):
                catalog.validate(value)
        with self.assertRaisesRegex(ValueError, "IMAGE_TAG"):
            catalog.verify_baseline(self.value, {**self.prod, "IMAGE_TAG": "c" * 40})
        other = copy.deepcopy(self.value["sources"]["ga_2026_general"])
        other["source"]["source_id"] = "ga_other"
        self.value["sources"]["ga_other"] = other
        with self.assertRaisesRegex(ValueError, "one results owner"):
            catalog.validate(self.value)

    @unittest.skipIf(os.name == "nt", "Linux host deployment lock")
    def test_backup_reader_lock_blocks_registration_and_updates_are_atomic(self):
        with catalog.locked(self.path):
            with self.assertRaises(BlockingIOError):
                with catalog.locked(self.path, exclusive=True, nonblocking=True):
                    self.fail("Exclusive catalog lock should not be granted")
        catalog.update(self.path, lambda v: v.update(note="checked"))
        self.assertEqual(catalog.read(self.path)["generation"], 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_worker_timeout_removes_only_its_owned_container_and_keeps_secrets_out_of_argv(self):
        entry = self.value["sources"]["ga_2026_general"]
        with patch.object(containers.subprocess, "run") as run:
            run.side_effect = [
                subprocess.TimeoutExpired("docker", 2),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                containers.run_job(
                    entry, self.prod, self.config, ["scripts/run_expansion_source.py"], timeout=2
                )
        first = run.call_args_list[0]
        args = first.args[0]
        self.assertNotIn("writer-secret", str(args))
        self.assertEqual(first.kwargs["env"]["POSTGRES_PASSWORD"], "writer-secret")
        self.assertIn(IMAGE, args)
        self.assertIn("--memory", args)
        self.assertIn("--cpus", args)
        self.assertIn("--read-only", args)
        self.assertEqual(run.call_args_list[1].args[0][-1], args[args.index("--name") + 1])
        self.assertNotIn("compose", args)
        self.assertNotIn("administrator-secret", str(first))

    def test_missing_resource_budget_fails_before_starting_a_container(self):
        with patch.object(containers, "command") as command:
            with self.assertRaises(ValueError):
                containers.run_job(self.value["sources"]["ga_2026_general"], self.prod, {}, [])
        command.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_failed_expansion_does_not_run_or_reconfigure_baseline(self):
        self.value["sources"]["ga_2026_general"]["enabled"] = True
        other = copy.deepcopy(self.value["sources"]["ga_2026_general"])
        other["source"].update(source_id="pa_2026_general", state="PA")
        self.value["sources"]["pa_2026_general"] = other
        catalog.write(self.path, self.value)
        before = self.path.read_bytes()
        with patch.object(
            control,
            "run_one",
            side_effect=[
                RuntimeError("writer-secret one source failed"),
                {"source_id": "pa_2026_general", "outcome": "succeeded"},
            ],
        ) as run:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(control.run_enabled(self.path, self.prod, self.config), 1)
        self.assertEqual([c.args[0]["source"]["state"] for c in run.call_args_list], ["GA", "PA"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("writer-secret", (self.root / "expansion-latest.json").read_text())
        self.assertEqual(
            json.loads((self.root / "expansion-latest.json").read_text())["outcome"], "failed"
        )

    def test_parallel_expansion_jobs_run_at_the_same_time(self):
        self.value["sources"]["ga_2026_general"]["enabled"] = True
        other = copy.deepcopy(self.value["sources"]["ga_2026_general"])
        other["source"].update(source_id="pa_2026_general", state="PA")
        self.value["sources"]["pa_2026_general"] = other
        catalog.write(self.path, self.value)
        # Neither job can finish until both have started, so a sequential run would stall.
        started = threading.Barrier(2, timeout=10)

        def run(entry, prod, config):
            started.wait()
            return {"source_id": entry["source"]["source_id"], "outcome": "succeeded"}

        with patch.object(control, "run_one", side_effect=run):
            with redirect_stdout(io.StringIO()):
                config = {**self.config, "EXPANSION_PARALLEL": "2"}
                self.assertEqual(control.run_enabled(self.path, self.prod, config), 0)
        latest = json.loads((self.root / "expansion-latest.json").read_text())
        self.assertEqual(
            sorted(r["source_id"] for r in latest["results"]),
            ["ga_2026_general", "pa_2026_general"],
        )

    def test_parallel_expansion_jobs_require_a_bounded_count(self):
        for value in ("0", "5", "two"):
            with patch.object(control, "run_one") as run, self.assertRaises(ValueError):
                control.run_enabled(
                    self.path, self.prod, {**self.config, "EXPANSION_PARALLEL": value}
                )
            run.assert_not_called()

    def test_only_new_schema_additive_sql_passes(self):
        body = migrations.validate_migration(ddl(), "ga")
        self.assertNotIn("COMMIT", body)
        self.assertIn("CREATE SCHEMA ga", body)
        mutations = (
            ddl().replace("CREATE SCHEMA ga", "CREATE SCHEMA nc"),
            ddl().replace("CREATE TABLE ga.results", "CREATE TABLE nc.results"),
            ddl().replace("COMMIT;", "DROP TABLE nc.results; COMMIT;"),
            ddl().replace("COMMIT;", "ALTER TABLE nc.results ADD x integer; COMMIT;"),
            ddl().replace("COMMIT;", "DO $$BEGIN NULL; END$$; COMMIT;"),
            ddl().replace(
                "COMMIT;", "UPDATE serving.current_publications SET source_id='x'; COMMIT;"
            ),
            ddl().replace("btrim(choice)", "pg_sleep(30)::text"),
            ddl().replace(
                "GRANT SELECT ON ga.contest_candidate_results", "GRANT SELECT ON ga.results"
            ),
            ddl().replace("REFERENCES ops.processing_releases", "REFERENCES nc.results"),
        )
        for text in mutations:
            with self.subTest(text=text[-200:]):
                with self.assertRaises(ValueError):
                    migrations.validate_migration(text, "ga")

    def test_migration_checksum_drift_and_contention_fail_before_ddl(self):
        path = self.root / "005_ga.sql"
        path.write_text(ddl(), encoding="utf-8")
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [{"ok": False}]
        with self.assertRaisesRegex(ValueError, "Another migration"):
            migrations.apply(conn, path, "ga")
        self.assertFalse(any("CREATE TABLE" in str(c) for c in conn.execute.call_args_list))
        conn.reset_mock()
        conn.execute.return_value.fetchone.side_effect = [{"ok": True}, {"sha256": "changed"}]
        with self.assertRaisesRegex(ValueError, "checksum"):
            migrations.apply(conn, path, "ga")
        statements = [c.args[0] for c in conn.execute.call_args_list]
        self.assertIn("SET LOCAL lock_timeout='750ms'", statements)
        self.assertNotIn("SET ROLE", str(statements))

    def test_recovery_gate_requires_new_state_and_same_publication(self):
        now = datetime.now(timezone.utc)
        entry = self.value["sources"]["ga_2026_general"]
        entry.update(
            publication_id="publication-ga", published_at=(now - timedelta(minutes=3)).isoformat()
        )
        report = {
            "outcome": "passed",
            "temporary_database_removed": True,
            "writer_permissions_verified": True,
            "reader_permissions_verified": True,
            "finished_at": (now - timedelta(minutes=1)).isoformat(),
            "database_inventory": {"ga.contest_candidate_results": {"rows": 3}},
            "current_publications": {"ga_2026_general": "publication-ga"},
        }
        control.recovery_gate(entry, report, now)
        for changes in (
            {"current_publications": {}},
            {"database_inventory": {}},
            {"finished_at": (now - timedelta(hours=2)).isoformat()},
            {"reader_permissions_verified": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                control.recovery_gate(entry, {**report, **changes}, now)

    def test_catalog_backup_covers_future_schema_and_locks_only_registered_sources(self):
        config = self.root / "sources.toml"
        config.write_text("# copied configuration", encoding="utf-8")
        for entry in self.value["sources"].values():
            entry["release_sha256"] = "c" * 64
        conn = MagicMock()
        conn.__enter__.return_value = conn

        def execute(statement, *args):
            if "pg_try_advisory_lock" in statement:
                return MagicMock(fetchone=lambda: {"ok": True})
            if "pg_export_snapshot" in statement:
                return MagicMock(fetchone=lambda: {"id": "snapshot"})
            return MagicMock(
                fetchall=lambda: [
                    {"schema_name": name}
                    for name in ("ops", "landing", "serving", "nc", "ky", "ga")
                ]
                if "information_schema.schemata" in statement
                else []
            )

        conn.execute.side_effect = execute

        def dump(root, service, db, program, arguments, **kwargs):
            if program == "pg_dump":
                self.assertIn("--schema=ga", arguments)
                kwargs["stdout"].write(b"PGDMP")
            return ""

        unregistered = Source("az_pending", "AZ", "2026-11-03", "https://example.com/az.zip")
        with (
            patch.object(
                recovery,
                "load_config",
                return_value=(CollectorSettings(), {"az_pending": unregistered}),
            ),
            patch.object(recovery, "connect", return_value=conn),
            patch.object(
                recovery, "inventory", return_value={"ga.results": {"rows": 0, "sha256": "abc"}}
            ) as inventory,
            patch.object(recovery, "docker_command", side_effect=dump),
            patch.object(recovery.subprocess, "check_output", return_value=REVISION + "\n"),
            patch.object(containers, "release_hash", return_value="c" * 64),
        ):
            result = recovery.backup(
                self.root,
                self.root / "data",
                config,
                {"dbname": "elections", "user": "admin", "password": "secret"},
                self.root / "backups",
                "db",
                catalog=self.value,
            )
        self.assertIn("ga", inventory.call_args.args[1])
        self.assertTrue((self.root / "data/locks/ga_2026_general.pipeline.lock").is_file())
        self.assertFalse((self.root / "data/locks/az_pending.lock").exists())
        manifest = recovery.verify_bundle(Path(result["backup_dir"]))
        self.assertIn("ga", manifest["database_schemas"])
        self.assertIn("deployment-catalog.json", manifest["files"])

    def test_dashboard_filters_pending_states_and_fails_closed_on_invalid_catalog(self):
        providers = {"NC": lambda: {"publication": None}, "KY": lambda: {"publication": None}}
        client = TestClient(create_app(state_providers=providers, catalog_file=self.path))
        result = client.get("/api/national")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()["coverage"]), {"NC", "KY"})
        self.assertEqual(client.get("/api/states/GA").status_code, 404)
        self.path.write_text("invalid json", encoding="utf-8")
        self.assertEqual(client.get("/api/national").status_code, 503)
        self.assertEqual(client.get("/api/states/NC").status_code, 503)

    def test_caddy_reload_failure_restores_file_and_never_stops_a_container(self):
        path = self.root / "Caddyfile"
        original = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        path.write_text(original, encoding="utf-8")
        inode = path.stat().st_ino
        with patch.object(
            dashboard, "reload", side_effect=[RuntimeError("reload failed"), None]
        ) as reload:
            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                dashboard.handoff(
                    path,
                    "caddy-id",
                    "election-expansion-dashboard-" + "f" * 32 + ":8000",
                    self.root / "handoff.json",
                )
        self.assertEqual(path.read_text(), original)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(reload.call_args.args[1], original)
        self.assertEqual(
            json.loads((self.root / "handoff.json").read_text())["outcome"], "rolled_back"
        )

    def test_caddy_persistence_failure_restores_live_route(self):
        path = self.root / "Caddyfile"
        original = (ROOT / "deploy/Caddyfile").read_text()
        path.write_text(original)
        with (
            patch.object(dashboard, "reload") as reload,
            patch.object(dashboard, "persist", side_effect=[OSError("disk failed"), None]),
        ):
            with self.assertRaises(OSError):
                dashboard.handoff(path, "caddy-id", "dashboard:8000", self.root / "handoff.json")
        self.assertEqual(reload.call_args.args[1], original)

    def test_active_and_previous_dashboard_cannot_be_discarded(self):
        name = "election-expansion-dashboard-" + "f" * 32
        self.value["previous_dashboard"] = {"name": name}
        with patch.object(dashboard.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                dashboard.stop_unused(name, self.value)
        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_failed_origin_check_rolls_back_route_and_catalog_together(self):
        path = self.root / "Caddyfile"
        original = (ROOT / "deploy/Caddyfile").read_text()
        path.write_text(original)
        after = copy.deepcopy(self.value)
        after["sources"]["ga_2026_general"].update(enabled=True, public=True)
        before = self.path.read_bytes()
        with (
            patch.object(dashboard, "reload") as reload,
            patch.object(dashboard, "check", side_effect=ValueError("unhealthy new origin")),
        ):
            with self.assertRaisesRegex(ValueError, "unhealthy"):
                control.switch_dashboard(
                    self.path,
                    self.value,
                    after,
                    "election-expansion-dashboard-" + "f" * 32,
                    self.config,
                )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(reload.call_args.args[1], original)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_pending_handoff_recovery_restores_catalog_and_route(self):
        path = self.root / "Caddyfile"
        original = (ROOT / "deploy/Caddyfile").read_text()
        path.write_text(original)
        journal = self.root / "handoff.json"
        after = copy.deepcopy(self.value)
        after["sources"]["ga_2026_general"].update(enabled=True, public=True)
        with patch.object(dashboard, "reload"):
            dashboard.handoff(
                path,
                "caddy-id",
                "election-expansion-dashboard-" + "f" * 32 + ":8000",
                journal,
                before=self.value,
                catalog_file=self.path,
            )
            catalog.write(self.path, after)
            self.assertEqual(json.loads(journal.read_text())["outcome"], "pending")
            dashboard.recover(journal)
        self.assertEqual(catalog.public_states(catalog.read(self.path)), {"NC", "KY"})
        self.assertEqual(path.read_text(), original)

    @unittest.skipIf(os.name == "nt", "Linux host deployment lock")
    def test_install_changes_only_maintenance_dropins_and_new_expansion_units(self):
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").touch()
        (self.root / "deploy/systemd").mkdir(parents=True)
        for name in ("election-expansion.service", "election-expansion.timer"):
            (self.root / "deploy/systemd" / name).write_bytes(
                (ROOT / "deploy/systemd" / name).read_bytes()
            )
        unit_root = self.root / "units"
        unit_root.mkdir()
        original = unit_root / "election-pipeline.service"
        original.write_text("frozen worker")
        with patch.object(containers, "command") as command:
            control.install_controls(self.config, unit_root)
        self.assertEqual(original.read_text(), "frozen worker")
        command.assert_called_once_with(["systemctl", "daemon-reload"])
        self.assertIn(
            "--catalog-file", (unit_root / "election-backup.service.d/deployment.conf").read_text()
        )

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_same_coverage_handoff_needs_no_repeat_of_existing_restore_gate(self):
        path = self.root / "Caddyfile"
        path.write_text((ROOT / "deploy/Caddyfile").read_text())
        identity = "f" * 32
        candidate = {
            "id": identity,
            "name": "election-expansion-dashboard-" + identity,
            "catalog_generation": 0,
            "sources": [],
            "states": ["NC", "KY"],
            "url": "http://new",
        }
        with (
            patch.object(control, "controls_ready"),
            patch.object(dashboard, "reload"),
            patch.object(dashboard, "check", return_value={"states": {}}),
            patch.object(control, "recovery_gate") as gate,
        ):
            result = control.activate(self.path, candidate, {}, self.prod, self.config)
        gate.assert_not_called()
        self.assertEqual(result["outcome"], "activated")
        value = catalog.read(self.path)
        self.assertEqual(value["dashboard"]["name"], candidate["name"])
        self.assertEqual(value["previous_dashboard"]["name"], "dashboard")
        self.assertFalse(value["sources"]["ga_2026_general"]["enabled"])
        self.assertEqual(json.loads((self.root / "handoff.json").read_text())["outcome"], "applied")


@unittest.skipUnless(
    os.environ.get("ELECTION_TEST_DATABASE"), "Requires native disposable PostgreSQL"
)
class DeploymentDatabaseTests(unittest.TestCase):
    def test_additive_migration_fails_fast_on_parent_lock_then_commits_without_changing_old_data(
        self,
    ):
        if not os.environ["ELECTION_TEST_DATABASE"].endswith("_test"):
            raise ValueError("Disposable *_test acceptance database required")
        db = connection_settings()
        name = "election_deployment_" + uuid.uuid4().hex[:16] + "_test"
        with connect({**db, "dbname": "postgres"}) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
        self.addCleanup(self.drop_database, db, name)
        db = {**db, "dbname": name}
        with connect(db) as conn:
            conn.execute("""
                CREATE SCHEMA ops; CREATE SCHEMA serving; CREATE SCHEMA nc;
                CREATE TABLE ops.processing_releases(release_id bigint PRIMARY KEY);
                CREATE TABLE ops.schema_migrations(name text PRIMARY KEY, sha256 text NOT NULL);
                CREATE TABLE serving.publications(publication_id uuid PRIMARY KEY);
                CREATE TABLE nc.results(total_votes bigint NOT NULL);
                INSERT INTO nc.results VALUES (47);
                DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='election_dashboard')
                THEN CREATE ROLE election_dashboard NOLOGIN; END IF; END $$;
            """)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "005_ga.sql"
            path.write_text(ddl(), encoding="utf-8")
            with connect(db) as other, other.transaction(), connect(db) as conn:
                other.execute("LOCK TABLE ops.processing_releases IN ACCESS EXCLUSIVE MODE")
                with self.assertRaises(Exception) as error:
                    migrations.apply(conn, path, "ga")
                self.assertIn("lock timeout", str(error.exception))
                self.assertIsNone(conn.execute("SELECT to_regnamespace('ga') AS n").fetchone()["n"])
            with connect(db) as conn:
                self.assertEqual(migrations.apply(conn, path, "ga")["outcome"], "applied")
                self.assertEqual(migrations.apply(conn, path, "ga")["outcome"], "already_applied")
                self.assertEqual(
                    conn.execute("SELECT total_votes FROM nc.results").fetchone()["total_votes"], 47
                )
                with conn.transaction(force_rollback=True):
                    conn.execute("SET LOCAL ROLE election_dashboard")
                    conn.execute("SELECT count(*) FROM ga.contest_candidate_results")
                    self.assertFalse(
                        conn.execute(
                            "SELECT has_table_privilege(current_user,'ga.results','SELECT') AS ok"
                        ).fetchone()["ok"]
                    )

    @staticmethod
    def drop_database(db, name):
        with connect({**db, "dbname": "postgres"}) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    unittest.main()


NEW_IMAGE = "sha256:" + "e" * 64
NEW_REVISION = "f" * 40


class RepinTests(unittest.TestCase):
    """An active expansion source moves to a verified image; nothing else changes."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        value = fixture(self.root)
        value["sources"]["ga_2026_general"].update(enabled=True, public=True)
        value["sources"]["ga_2026_general"]["release_sha256"] = "c" * 64
        catalog.write(self.path, value)
        self.before = catalog.read(self.path)["sources"]["ga_2026_general"]
        self.prod = {"DATA_DIR": str(self.root / "data")}
        self.config = {"EXPANSION_CPUS": "0.5", "EXPANSION_MEMORY": "768m"}
        self.release = self.root / "release-new"
        self.prepared = []

    def metadata(self, **changes):
        meta = {
            "code_sha256": "code",
            "source": copy.deepcopy(self.before["source"]),
            "settings": copy.deepcopy(self.before["settings"]),
            "races": list(self.before["races"]),
        }
        for key, value in changes.items():
            if key in ("url", "source_id"):
                meta["source"][key] = value
            elif key == "timeout_seconds":
                meta["settings"][key] = value
            else:
                meta[key] = value
        return meta

    def repin(
        self,
        *,
        image=NEW_IMAGE,
        revision=NEW_REVISION,
        meta=None,
        prepared=None,
        backup="inactive",
        name="ga_2026_general",
    ):
        def run_one(entry, prod, config, *, prepare=False):
            self.prepared.append((entry["image_id"], prepare))
            if callable(prepared):
                return prepared()
            return prepared or {
                "source_id": name,
                "outcome": "validated",
                "candidate_id": str(uuid.uuid4()),
            }

        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "require_applied_migrations"),
            patch.object(containers, "verify_release", return_value=(image, revision)),
            patch.object(containers, "metadata", return_value=meta or self.metadata()),
            patch.object(containers, "package_hash", return_value="code"),
            patch.object(containers, "release_hash", return_value="d" * 64),
            patch.object(containers, "command", return_value=backup),
            patch.object(control, "run_one", side_effect=run_one),
        ):
            return control.repin(self.path, self.release, image, name, self.prod, self.config)

    def entry(self):
        return catalog.read(self.path)["sources"]["ga_2026_general"]

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_validated_new_image_changes_only_the_pin_and_keeps_history(self):
        generation = catalog.read(self.path)["generation"]
        result = self.repin()
        after = self.entry()
        self.assertEqual(result["outcome"], "repinned")
        self.assertEqual(self.prepared, [(NEW_IMAGE, True)])
        self.assertEqual(
            {k: after[k] for k in control.PIN_KEYS},
            {
                "image_id": NEW_IMAGE,
                "revision": NEW_REVISION,
                "release_path": str(self.release.resolve()),
                "release_sha256": "d" * 64,
            },
        )
        for key in ("source", "settings", "races", "status", "enabled", "public", "worker"):
            self.assertEqual(after[key], self.before[key], key)
        self.assertEqual(
            {k: after["previous_images"][0][k] for k in control.PIN_KEYS},
            {k: self.before[k] for k in control.PIN_KEYS},
        )
        self.assertEqual(after["repin_candidate_id"], result["validated_candidate_id"])
        self.assertEqual(catalog.read(self.path)["generation"], generation + 1)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_rollback_is_a_repin_to_the_previous_image(self):
        self.repin()
        result = self.repin(image=IMAGE, revision=REVISION)
        after = self.entry()
        self.assertEqual(result["previous_revision"], NEW_REVISION)
        self.assertEqual((after["image_id"], after["revision"]), (IMAGE, REVISION))
        self.assertEqual([p["image_id"] for p in after["previous_images"]], [IMAGE, NEW_IMAGE])

    def test_same_image_is_a_no_op_without_a_run(self):
        result = self.repin(image=IMAGE, revision=REVISION)
        self.assertEqual(result["outcome"], "already_pinned")
        self.assertEqual(self.prepared, [])
        self.assertEqual(self.entry(), self.before)

    def test_contract_settings_or_race_changes_are_refused_before_any_run(self):
        for changes in (
            {"url": "https://example.com/moved.zip"},
            {"timeout_seconds": 5},
            {"races": ["GA-SEN", "GA-01"]},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "cannot change"):
                self.repin(meta=self.metadata(**changes))
        self.assertEqual(self.prepared, [])
        self.assertEqual(self.entry(), self.before)

    def test_failed_validation_keeps_the_old_pin(self):
        failure = {"source_id": "ga_2026_general", "outcome": "failed", "error": "reset"}
        with self.assertRaisesRegex(ValueError, "did not produce a validated candidate: reset"):
            self.repin(prepared=failure)
        self.assertEqual(self.entry(), self.before)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_only_expansion_sources_and_no_concurrent_change_or_backup(self):
        with self.assertRaisesRegex(ValueError, "restricted to new expansion sources"):
            self.repin(name="nc_2026_general", meta=self.metadata(source_id="nc_2026_general"))

        def raced():
            catalog.update(
                self.path,
                lambda v: v["sources"]["ga_2026_general"].update(revision="9" * 40),
            )
            return {
                "source_id": "ga_2026_general",
                "outcome": "validated",
                "candidate_id": str(uuid.uuid4()),
            }

        with self.assertRaisesRegex(ValueError, "changed during re-pin"):
            self.repin(prepared=raced)
        self.assertEqual(self.entry()["image_id"], IMAGE)
        with self.assertRaisesRegex(ValueError, "backup is running"):
            self.repin(backup="active")
        self.assertEqual(self.entry()["image_id"], IMAGE)
