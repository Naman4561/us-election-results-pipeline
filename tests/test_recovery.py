"""Backup integrity, binary transport, restore isolation and job quiescing."""

import contextlib
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from election_pipeline import recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        self.root = Path(temporary.name)
        self.db = {
            "dbname": "elections",
            "user": "admin",
            "password": "secret",
        }

        self.dump = self.root / "database.dump"
        self.dump.write_bytes(b"PGDMP\x00\xff\r\n")

        raw = self.root / "data/raw/example.zip"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"raw bytes")

        self.inventory = {
            "ops.sources": {
                "rows": 1,
                "sha256": "test-fingerprint",
            }
        }

        manifest = {
            "schema_version": 1,
            "database_inventory": self.inventory,
            "files": {},
        }

        for path in (self.dump, raw):
            manifest["files"][path.relative_to(self.root).as_posix()] = {
                "bytes": path.stat().st_size,
                "sha256": recovery.file_hash(path),
            }

        (self.root / "manifest.json").write_text(json.dumps(manifest))

    def test_binary_stream_has_no_shell_or_password_argument(self):
        completed = subprocess.CompletedProcess([], 0, b"", b"")

        with patch.object(subprocess, "run", return_value=completed) as run:
            with self.dump.open("rb") as handle:
                recovery.docker_command(
                    self.root,
                    "postgres",
                    self.db,
                    "pg_restore",
                    ["--list"],
                    stdin=handle,
                )

                self.assertIs(run.call_args.kwargs["stdin"], handle)

        command = run.call_args.args[0]

        self.assertEqual(
            command[:6],
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
            ],
        )
        self.assertNotIn("secret", str(command))
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_manifest_rejects_changed_dump_and_traversal(self):
        recovery.verify_bundle(self.root)
        self.dump.write_bytes(b"damaged")

        with self.assertRaisesRegex(ValueError, "checksum"):
            recovery.verify_bundle(self.root)

        for relative in (
            "../secret",
            "/etc/passwd",
            "C:\\secret",
            "data/../../secret",
        ):
            with self.subTest(relative=relative):
                with self.assertRaises(ValueError):
                    recovery.safe_child(self.root, relative)

    def test_restore_creates_and_drops_only_its_own_new_database(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        snapshots = [
            {
                "raw_path": "raw/example.zip",
                "raw_sha256": hashlib.sha256(b"raw bytes").hexdigest(),
            }
        ]
        conn.execute.side_effect = lambda statement, *args: MagicMock(
            fetchall=lambda: []
            if "SELECT source_id, publication_id" in str(statement)
            else snapshots,
            fetchone=lambda: {"ok": False},
        )
        conn.execute.return_value.fetchone.return_value = {"ok": False}

        with (
            patch.object(recovery, "connect", return_value=conn),
            patch.object(
                recovery,
                "inventory",
                return_value=self.inventory,
            ),
            patch.object(recovery, "docker_command") as command,
        ):
            result = recovery.check_restore(
                self.root,
                self.db,
                self.root,
                "postgres",
            )

        self.assertEqual(result["outcome"], "passed", result)

        name = result["temporary_database"]

        self.assertTrue(name.startswith("election_restore_") and name.endswith("_test"))
        self.assertNotEqual(name, self.db["dbname"])

        statements = [
            call.args[0].as_string()
            for call in conn.execute.call_args_list
            if not isinstance(call.args[0], str)
        ]

        self.assertEqual(
            statements,
            [
                f'CREATE DATABASE "{name}" TEMPLATE template0',
                f'DROP DATABASE "{name}"',
            ],
        )
        self.assertEqual(command.call_args.args[2]["dbname"], name)
        self.assertTrue(result["temporary_database_removed"])
        self.assertEqual(result["raw_archives_verified"], 1)

    def test_failed_creation_never_drops_an_existing_database(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.side_effect = RuntimeError("creation failed")

        with patch.object(recovery, "connect", return_value=conn):
            result = recovery.check_restore(
                self.root,
                self.db,
                self.root,
                "postgres",
            )

        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(conn.execute.call_count, 1)
        self.assertNotIn("temporary_database_removed", result)

    def test_restore_uses_manifest_schemas_and_verifies_future_state_reader_access(self):
        manifest = json.loads((self.root / "manifest.json").read_text())
        manifest["database_schemas"] = ["ops", "landing", "serving", "ga"]
        expected = {
            **self.inventory,
            "ga.contest_candidate_results": {"rows": 3, "sha256": "abc"},
            "ga.results": {"rows": 3, "sha256": "def"},
        }
        manifest["database_inventory"] = expected
        (self.root / "manifest.json").write_text(json.dumps(manifest))
        conn = MagicMock()
        conn.__enter__.return_value = conn

        def execute(statement, *args):
            statement = statement if isinstance(statement, str) else statement.as_string()
            return MagicMock(
                fetchone=lambda: {"ok": False},
                fetchall=lambda: (
                    [{"source_id": "ga_2026_general", "publication_id": "future-publication"}]
                    if "SELECT source_id, publication_id" in statement
                    else []
                ),
            )

        conn.execute.side_effect = execute
        with (
            patch.object(recovery, "connect", return_value=conn),
            patch.object(recovery, "inventory", return_value=expected) as inventory,
            patch.object(recovery, "docker_command"),
        ):
            result = recovery.check_restore(self.root, self.db, self.root, "postgres")
        self.assertEqual(result["outcome"], "passed", result)
        self.assertIn("ga", inventory.call_args.args[1])
        self.assertEqual(result["current_publications"]["ga_2026_general"], "future-publication")
        statements = [
            c.args[0] if isinstance(c.args[0], str) else c.args[0].as_string()
            for c in conn.execute.call_args_list
        ]
        self.assertIn(
            'GRANT SELECT ON "ga"."contest_candidate_results" TO election_dashboard', statements
        )
        self.assertIn('SELECT count(*) FROM "ga"."contest_candidate_results"', statements)
        self.assertNotIn('GRANT SELECT ON "ga"."results" TO election_dashboard', statements)

    def test_bad_bundle_cannot_create_a_database(self):
        self.dump.write_bytes(b"bad")

        with patch.object(recovery, "connect") as connect:
            with self.assertRaises(ValueError):
                recovery.check_restore(
                    self.root,
                    self.db,
                    self.root,
                    "postgres",
                )

        connect.assert_not_called()


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/sources.toml"


class Result:
    def __init__(self, one=None, rows=()):
        self.one, self.rows = one, list(rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Enough of a psycopg connection for backup(); records its statements."""

    def __init__(self, advisory=(True,)):
        self.advisory = list(advisory)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if "pg_try_advisory_lock" in statement:
            return Result(
                {"ok": self.advisory.pop(0) if len(self.advisory) > 1 else self.advisory[0]}
            )
        if "pg_export_snapshot" in statement:
            return Result({"id": "00000003-1"})
        return Result(rows=())

    def transaction(self):
        return contextlib.nullcontext()


class QuiesceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        _, self.sources = recovery.load_config(CONFIG)
        self.first, self.last = (
            recovery.ArchiveStore(self.data, self.sources[name])
            for name in (min(self.sources), max(self.sources))
        )
        patcher = patch.object(recovery, "QUIESCE_RETRY_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waits_for_a_running_job_then_holds_every_source(self):
        held, release = threading.Event(), threading.Event()

        def job():
            with self.last.lock(stage="pipeline"):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=job)
        worker.start()
        held.wait(5)
        threading.Timer(0.3, release.set).start()
        conn = FakeConnection()

        with recovery.quiesced(self.data, self.sources, conn, wait=5):
            self.assertTrue(release.is_set())
            with self.assertRaises(recovery.CollectionLocked):
                with self.first.lock():
                    pass
        worker.join(5)

        with self.first.lock(), self.last.lock(stage="pipeline"):
            pass
        self.assertIn("SELECT pg_advisory_unlock(20261103, 1)", conn.statements)

    def test_never_holds_some_sources_while_waiting_and_gives_up(self):
        with self.last.lock():
            with self.assertRaisesRegex(ValueError, "Jobs still running"):
                with recovery.quiesced(self.data, self.sources, FakeConnection(), wait=0.2):
                    self.fail("Entered while a job ran")
            # The sources before the busy one were released between tries.
            with self.first.lock(), self.first.lock(stage="pipeline"):
                pass

    def test_waits_for_a_running_publication(self):
        conn = FakeConnection(advisory=(False, False, True))
        with recovery.quiesced(self.data, self.sources, conn, wait=5):
            pass
        self.assertEqual(sum("pg_try_advisory_lock" in s for s in conn.statements), 3)

    def test_jobs_run_during_the_long_copy_and_dump(self):
        name = min(self.sources)
        state = self.data / "state" / f"{name}.json"
        latest = self.data / "runs" / name / "latest.json"
        report = self.data / "runs" / name / "old-run.json"
        raw = self.data / "raw" / "XX" / "archive.zip"
        for path, text in ((state, "before"), (latest, "before"), (report, "run"), (raw, "raw")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        conn = FakeConnection()

        def dump(root, service, db, program, arguments, *, stdout=None, **kwargs):
            if program == "pg_dump":
                # A job runs now: it takes its source's locks and rewrites its files.
                with self.first.lock(stage="pipeline"), self.first.lock():
                    state.write_text("after")
                    latest.write_text("after")
                    (latest.parent / "new-run.json").write_text("new")
                stdout.write(b"PGDMP")
            return ""

        with (
            patch.object(recovery, "connect", return_value=contextlib.nullcontext(conn)),
            patch.object(recovery, "inventory", return_value={}),
            patch.object(recovery, "docker_command", side_effect=dump),
        ):
            result = recovery.backup(
                ROOT, self.data, CONFIG, {"dbname": "elections"}, self.root / "out", "db"
            )

        bundle = Path(result["backup_dir"])
        # Files rewritten in place keep the state that matches the database snapshot.
        self.assertEqual((bundle / "data" / "state" / f"{name}.json").read_text(), "before")
        self.assertEqual((bundle / "data" / "runs" / name / "latest.json").read_text(), "before")
        # Files written once go into the packs, and only those listed with the snapshot.
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(set(manifest["data"]), {f"runs/{name}/old-run.json", "raw/XX/archive.zip"})
        restored = self.root / "restored"
        for pack in result["new_packs"]:
            recovery.unpack_pack(manifest, bundle / "packs" / pack, restored)
        self.assertEqual((restored / "runs" / name / "old-run.json").read_text(), "run")
        self.assertEqual((restored / "raw" / "XX" / "archive.zip").read_text(), "raw")
        self.assertFalse(any(name.startswith("packs/") for name in manifest["files"]))

    def backup(self, previous=None, snapshots=()):
        class Connection(FakeConnection):
            def execute(self, statement, params=None):
                if "FROM ops.snapshots" in statement:
                    return Result(rows=snapshots)
                return super().execute(statement, params)

        def dump(root, service, db, program, arguments, *, stdout=None, **kwargs):
            if stdout is not None:
                stdout.write(b"PGDMP")
            return ""

        with (
            patch.object(recovery, "connect", return_value=contextlib.nullcontext(Connection())),
            patch.object(recovery, "inventory", return_value={}),
            patch.object(recovery, "docker_command", side_effect=dump),
        ):
            result = recovery.backup(
                ROOT,
                self.data,
                CONFIG,
                {"dbname": "elections"},
                self.root / "out",
                "db",
                previous=previous,
            )
        bundle = Path(result["backup_dir"])
        return result, json.loads((bundle / "manifest.json").read_text())

    def test_a_backup_packs_only_the_files_new_since_the_previous_one(self):
        old, new = self.data / "raw/XX/old.zip", self.data / "publications/p2/run.json"
        for path in (old, new):
            path.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"old raw")
        first, previous = self.backup()
        self.assertEqual(len(first["new_packs"]), 1)
        new.write_text("{}")
        second, manifest = self.backup(previous)
        # The raw archive stays in the first backup's pack; only the new file is packed.
        self.assertEqual(manifest["data"]["raw/XX/old.zip"], previous["data"]["raw/XX/old.zip"])
        self.assertEqual(len(second["new_packs"]), 1)
        self.assertTrue(second["new_packs"][0].endswith("-files.zip"))
        self.assertEqual(set(manifest["packs"]), set(first["new_packs"] + second["new_packs"]))
        # A file changed since (same path, new content) is packed again.
        old.write_bytes(b"rewritten raw archive")
        third, manifest = self.backup(manifest)
        self.assertEqual(manifest["data"]["raw/XX/old.zip"]["bytes"], 21)
        self.assertTrue(third["new_packs"][0].endswith("-raw.zip"))

    def test_a_pruned_file_is_left_out_and_a_lost_raw_archive_fails_the_backup(self):
        raw = self.data / "raw/XX/archive.zip"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"raw")
        snapshots = [{"raw_path": "raw/XX/archive.zip", "raw_sha256": recovery.file_hash(raw)}]
        _, manifest = self.backup(snapshots=snapshots)
        self.assertIn("raw/XX/archive.zip", manifest["data"])
        with self.assertRaisesRegex(ValueError, "raw archive"):
            self.backup(snapshots=[{**snapshots[0], "raw_sha256": "0" * 64}])

        listed = {"publications": [Path("gone/manifest.json")], "raw": []}
        data, packs = recovery.pack_files(self.data, listed, self.root / "packs", "backup_x")
        self.assertEqual((data, packs), ({}, {}))
        with self.assertRaises(FileNotFoundError):
            recovery.pack_files(
                self.data, {"metadata": [Path("gone.json")]}, self.root / "packs", "backup_y"
            )

    def test_a_tampered_pack_or_file_is_rejected(self):
        raw = self.data / "raw/XX/archive.zip"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"raw")
        result, manifest = self.backup()
        pack = Path(result["backup_dir"]) / "packs" / result["new_packs"][0]
        tampered = json.loads(json.dumps(manifest))
        tampered["data"]["raw/XX/archive.zip"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Packed file"):
            recovery.unpack_pack(tampered, pack, self.root / "one")
        pack.write_bytes(pack.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "Pack does not match"):
            recovery.unpack_pack(manifest, pack, self.root / "two")

    def test_a_failed_backup_leaves_no_unfinished_bundle(self):
        raw = self.data / "raw" / "XX" / "archive.zip"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("raw")
        out = self.root / "out"
        # As on Nov 10, 2026: the data is copied, then a database statement fails.
        with (
            patch.object(
                recovery, "connect", return_value=contextlib.nullcontext(FakeConnection())
            ),
            patch.object(recovery, "inventory", side_effect=RuntimeError("statement timeout")),
            self.assertRaisesRegex(RuntimeError, "statement timeout"),
        ):
            recovery.backup(ROOT, self.data, CONFIG, {"dbname": "elections"}, out, "db")
        self.assertEqual(list(out.iterdir()), [])


class InventoryTests(unittest.TestCase):
    def test_fingerprinting_lifts_the_statement_limit_for_its_transaction_only(self):
        conn = FakeConnection()
        with self.assertRaisesRegex(ValueError, "migrations"):  # no tables in the fake
            recovery.inventory(conn)
        self.assertEqual(conn.statements[0], "SET LOCAL statement_timeout = 0")
