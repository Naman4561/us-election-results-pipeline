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
            manifest["files"][
                path.relative_to(self.root).as_posix()
            ] = {
                "bytes": path.stat().st_size,
                "sha256": recovery.file_hash(path),
            }

        (self.root / "manifest.json").write_text(
            json.dumps(manifest)
        )

    def test_binary_stream_has_no_shell_or_password_argument(self):
        completed = subprocess.CompletedProcess(
            [], 0, b"", b""
        )

        with patch.object(
            subprocess, "run", return_value=completed
        ) as run:
            with self.dump.open("rb") as handle:
                recovery.docker_command(
                    self.root,
                    "postgres",
                    self.db,
                    "pg_restore",
                    ["--list"],
                    stdin=handle,
                )

                self.assertIs(
                    run.call_args.kwargs["stdin"], handle
                )

        command = run.call_args.args[0]

        self.assertEqual(
            command[:6],
            [
                "docker", "compose", "exec",
                "-T", "postgres", "pg_restore",
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
        conn.execute.return_value.fetchall.return_value = [{
            "raw_path": "raw/example.zip",
            "raw_sha256": hashlib.sha256(
                b"raw bytes"
            ).hexdigest(),
        }]
        conn.execute.return_value.fetchone.return_value = {
            "ok": False
        }

        with (
            patch.object(
                recovery, "connect", return_value=conn
            ),
            patch.object(
                recovery,
                "inventory",
                return_value=self.inventory,
            ),
            patch.object(
                recovery, "docker_command"
            ) as command,
        ):
            result = recovery.check_restore(
                self.root,
                self.db,
                self.root,
                "postgres",
            )

        self.assertEqual(
            result["outcome"], "passed", result
        )

        name = result["temporary_database"]

        self.assertTrue(
            name.startswith("election_restore_")
            and name.endswith("_test")
        )
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
        self.assertEqual(
            command.call_args.args[2]["dbname"], name
        )
        self.assertTrue(
            result["temporary_database_removed"]
        )
        self.assertEqual(
            result["raw_archives_verified"], 1
        )

    def test_failed_creation_never_drops_an_existing_database(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.side_effect = RuntimeError(
            "creation failed"
        )

        with patch.object(
            recovery, "connect", return_value=conn
        ):
            result = recovery.check_restore(
                self.root,
                self.db,
                self.root,
                "postgres",
            )

        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(conn.execute.call_count, 1)
        self.assertNotIn(
            "temporary_database_removed", result
        )

    def test_bad_bundle_cannot_create_a_database(self):
        self.dump.write_bytes(b"bad")

        with patch.object(
            recovery, "connect"
        ) as connect:
            with self.assertRaises(ValueError):
                recovery.check_restore(
                    self.root,
                    self.db,
                    self.root,
                    "postgres",
                )

        connect.assert_not_called()