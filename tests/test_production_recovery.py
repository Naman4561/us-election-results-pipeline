"""Production backup integrity and restore routing tests."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from election_pipeline import recovery
from election_pipeline.storage import atomic_json, file_hash

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "production_recovery",
    ROOT / "scripts/production_recovery.py",
)
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


class ProductionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        self.bundle = self.root / ("backup_" + "a" * 32)
        self.bundle.mkdir()
        dump = self.bundle / "database.dump"
        dump.write_bytes(b"synthetic database archive\x00\xff")

        atomic_json(
            self.bundle / "manifest.json",
            {
                "schema_version": 1,
                "database_inventory": {
                    "ops.sources": {"rows": 1, "sha256": "fixture"}
                },
                "files": {
                    "database.dump": {
                        "bytes": dump.stat().st_size,
                        "sha256": file_hash(dump),
                    }
                },
            },
        )

        self.project = self.root / "project"
        self.project.mkdir()
        for name in (
            "Dockerfile", "compose.production.yml", "README.md"
        ):
            (self.project / name).write_text("fixture\n")

        with (
            patch.object(job, "ROOT", self.project),
            patch.object(
                job.subprocess,
                "check_output",
                return_value="abcdef1234567\n",
            ),
        ):
            self.archive, self.receipt = job.pack_bundle(
                self.bundle, "deployed-commit"
            )

    def test_round_trip_preserves_data_and_deployed_identity(self):
        destination = self.root / "download"
        job.unpack_bundle(
            self.archive, destination, self.receipt
        )
        manifest = recovery.verify_bundle(destination)
        self.assertEqual(
            manifest["deployment"]["image_tag"],
            "deployed-commit",
        )
        self.assertEqual(
            (destination / "database.dump").read_bytes(),
            b"synthetic database archive\x00\xff",
        )
        self.assertIn("Dockerfile", manifest["files"])

    def test_corrupt_download_is_rejected_before_extraction(self):
        self.archive.write_bytes(b"corrupt")
        destination = self.root / "download"
        with self.assertRaisesRegex(
            ValueError, "completion record"
        ):
            job.unpack_bundle(
                self.archive, destination, self.receipt
            )
        self.assertFalse(destination.exists())

    def test_traversal_is_rejected_with_matching_outer_checksum(self):
        with zipfile.ZipFile(self.archive, "w") as zipped:
            zipped.writestr("../escaped.txt", b"bad")

        receipt = {
            **self.receipt,
            "bytes": self.archive.stat().st_size,
            "sha256": file_hash(self.archive),
            "expanded_bytes": 3,
        }
        with self.assertRaises(ValueError):
            job.unpack_bundle(
                self.archive, self.root / "download", receipt
            )
        self.assertFalse(
            (self.root / "escaped.txt").exists()
        )

    def test_failed_upload_cannot_publish_completion_record(self):
        config = {
            "BACKUP_BUCKET": "private-bucket",
            "BACKUP_PREFIX": "election-pipeline",
        }
        with patch.object(
            job, "aws_copy",
            side_effect=RuntimeError("upload failed"),
        ) as copy:
            with self.assertRaises(RuntimeError):
                job.upload_bundle(
                    config, self.bundle,
                    self.archive, self.receipt,
                )

        self.assertEqual(copy.call_count, 1)
        self.assertTrue(
            copy.call_args.args[-1].endswith("bundle.zip")
        )

    def test_restore_downloads_fresh_copy_without_printing_bucket(self):
        prod_file = self.root / "production.env"
        prod_file.write_text(
            "POSTGRES_DB=elections\n"
            "POSTGRES_USER=admin\n"
            "POSTGRES_PASSWORD=secret\n"
            "PIPELINE_DB_USER=pipeline_writer\n"
            "DASHBOARD_DB_USER=dashboard_reader\n"
            f"DATA_DIR={self.root / 'live'}\n"
        )
        config_file = self.root / "backup.env"
        config_file.write_text(
            "BACKUP_BUCKET=private-bucket\n"
        )
        receipt_path = self.root / "receipt.json"
        atomic_json(receipt_path, self.receipt)

        def copy(config, source, destination):
            self.assertTrue(source.startswith(
                "s3://private-bucket/election-pipeline/rolling/"
            ))
            original = (
                receipt_path
                if source.endswith("complete.json")
                else self.archive
            )
            Path(destination).write_bytes(original.read_bytes())

        arguments = [
            "production_recovery.py",
            "check-restore",
            "--backup-id", self.bundle.name,
            "--env-file", str(prod_file),
            "--backup-env-file", str(config_file),
            "--destination", str(self.root / "work"),
        ]
        with (
            patch.object(sys, "argv", arguments),
            patch.object(
                job, "aws_copy", side_effect=copy
            ) as download,
            patch.object(
                job, "check_restore",
                return_value={"outcome": "passed"},
            ) as restore,
            patch.object(job.os, "umask"),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(job.main(), 0)

        self.assertEqual(download.call_count, 2)
        restored_bundle = restore.call_args.args[2]
        self.assertNotEqual(restored_bundle, self.bundle)
        self.assertEqual(
            (restored_bundle / "database.dump").read_bytes(),
            b"synthetic database archive\x00\xff",
        )
        self.assertEqual(
            restore.call_args.kwargs["writer_role"],
            "pipeline_writer",
        )
        self.assertNotIn(
            "private-bucket", output.getvalue()
        )

    def test_restore_uses_production_compose_and_writer_ownership(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn

        def execute(query, parameters=None):
            result = MagicMock()
            if (
                isinstance(query, str)
                and "pg_get_userbyid" in query
            ):
                result.fetchone.return_value = {
                    "owner": "pipeline_writer"
                }
            else:
                result.fetchone.return_value = {"ok": False}
            result.fetchall.return_value = []
            return result

        conn.execute.side_effect = execute
        db = {
            "dbname": "live_elections",
            "user": "admin",
            "password": "secret",
        }
        compose = (
            "--env-file", "/private/production.env",
            "--file", "/app/compose.production.yml",
        )
        manifest = json.loads(
            (self.bundle / "manifest.json").read_text()
        )

        with (
            patch.object(
                recovery, "connect", return_value=conn
            ),
            patch.object(
                recovery, "inventory",
                return_value=manifest["database_inventory"],
            ),
            patch.object(
                recovery, "docker_command"
            ) as transport,
        ):
            result = recovery.check_restore(
                self.root, db, self.bundle, "db",
                compose_args=compose,
                writer_role="pipeline_writer",
                reader_role="dashboard_reader",
            )

        self.assertEqual(
            result["outcome"], "passed", result
        )
        self.assertNotEqual(
            transport.call_args.args[2]["dbname"],
            "live_elections",
        )
        self.assertIn(
            "--role=pipeline_writer",
            transport.call_args.args[4],
        )
        self.assertEqual(
            transport.call_args.kwargs["compose_args"],
            compose,
        )
        self.assertTrue(
            result["temporary_database_removed"]
        )
        self.assertTrue(
            result["writer_permissions_verified"]
        )

    def test_local_marker_requires_both_remote_uploads(self):
        config = {
            "BACKUP_BUCKET": "private-bucket",
            "BACKUP_PREFIX": "election-pipeline",
        }
        marker = self.bundle / "uploaded.json"

        failures = (
            [RuntimeError("archive failed")],
            [None, RuntimeError("receipt failed")],
        )
        for responses in failures:
            with patch.object(
                job, "aws_copy", side_effect=responses
            ):
                with self.assertRaises(RuntimeError):
                    job.upload_bundle(
                        config, self.bundle,
                        self.archive, self.receipt,
                    )
            self.assertFalse(marker.exists())

        with patch.object(
            job, "aws_copy", return_value=None
        ):
            job.upload_bundle(
                config, self.bundle,
                self.archive, self.receipt,
            )

        self.assertEqual(
            json.loads(marker.read_text()),
            self.receipt,
        )


if __name__ == "__main__":
    unittest.main()