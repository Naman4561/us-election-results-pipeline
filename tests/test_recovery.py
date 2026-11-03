"""Backup integrity, binary transport and restore isolation."""

import hashlib
import json
import subprocess
import tempfile
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
