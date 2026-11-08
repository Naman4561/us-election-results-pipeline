"""Production backup integrity and restore routing tests."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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
                "database_inventory": {"ops.sources": {"rows": 1, "sha256": "fixture"}},
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
        for name in ("Dockerfile", "compose.production.yml", "README.md"):
            (self.project / name).write_text("fixture\n")

        with (
            patch.object(job, "ROOT", self.project),
            patch.object(
                job.subprocess,
                "check_output",
                return_value="abcdef1234567\n",
            ),
        ):
            self.archive, self.receipt = job.pack_bundle(self.bundle, "deployed-commit")

    def test_round_trip_preserves_data_and_deployed_identity(self):
        destination = self.root / "download"
        job.unpack_bundle(self.archive, destination, self.receipt)
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
        with self.assertRaisesRegex(ValueError, "completion record"):
            job.unpack_bundle(self.archive, destination, self.receipt)
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
            job.unpack_bundle(self.archive, self.root / "download", receipt)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_failed_upload_cannot_publish_completion_record(self):
        config = {
            "BACKUP_BUCKET": "private-bucket",
            "BACKUP_PREFIX": "election-pipeline",
        }
        with patch.object(
            job,
            "aws_copy",
            side_effect=RuntimeError("upload failed"),
        ) as copy:
            with self.assertRaises(RuntimeError):
                job.upload_bundle(
                    config,
                    self.bundle,
                    self.archive,
                    self.receipt,
                )

        self.assertEqual(copy.call_count, 1)
        self.assertTrue(copy.call_args.args[-1].endswith("bundle.zip"))

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
        config_file.write_text("BACKUP_BUCKET=private-bucket\n")
        receipt_path = self.root / "receipt.json"
        atomic_json(receipt_path, self.receipt)

        def copy(config, source, destination):
            self.assertTrue(source.startswith("s3://private-bucket/election-pipeline/rolling/"))
            original = receipt_path if source.endswith("complete.json") else self.archive
            Path(destination).write_bytes(original.read_bytes())

        arguments = [
            "production_recovery.py",
            "check-restore",
            "--backup-id",
            self.bundle.name,
            "--env-file",
            str(prod_file),
            "--backup-env-file",
            str(config_file),
            "--destination",
            str(self.root / "work"),
        ]
        for passed in (True, False):
            with self.subTest(passed=passed):
                report = self.root / "live/recovery_checks/restore.json"

                def restore_check(root, db, restored_bundle, service, **kwargs):
                    self.assertNotEqual(restored_bundle, self.bundle)
                    self.assertEqual(
                        (restored_bundle / "database.dump").read_bytes(),
                        b"synthetic database archive\x00\xff",
                    )
                    self.assertEqual(kwargs["writer_role"], "pipeline_writer")
                    self.assertEqual(kwargs["report_dir"], report.parent)
                    result = {
                        "outcome": "passed" if passed else "failed",
                        "temporary_database_removed": True,
                        "report_path": str(report),
                    }
                    atomic_json(report, result)
                    return result

                with (
                    patch.object(sys, "argv", arguments),
                    patch.object(job, "aws_copy", side_effect=copy) as download,
                    patch.object(job, "check_restore", side_effect=restore_check) as restore,
                    patch.object(job.os, "umask"),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(job.main(), 0 if passed else 1)

                self.assertEqual(download.call_count, 2)
                self.assertEqual(restore.call_args.args[2].parent.exists(), not passed)
                self.assertTrue(report.exists())
                self.assertTrue(self.bundle.exists())
                self.assertNotIn("private-bucket", output.getvalue())

    def test_restore_uses_production_compose_and_writer_ownership(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn

        def execute(query, parameters=None):
            result = MagicMock()
            if isinstance(query, str) and "pg_get_userbyid" in query:
                result.fetchone.return_value = {"owner": "pipeline_writer"}
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
            "--env-file",
            "/private/production.env",
            "--file",
            "/app/compose.production.yml",
        )
        manifest = json.loads((self.bundle / "manifest.json").read_text())

        with (
            patch.object(recovery, "connect", return_value=conn),
            patch.object(
                recovery,
                "inventory",
                return_value=manifest["database_inventory"],
            ),
            patch.object(recovery, "docker_command") as transport,
        ):
            result = recovery.check_restore(
                self.root,
                db,
                self.bundle,
                "db",
                compose_args=compose,
                writer_role="pipeline_writer",
                reader_role="dashboard_reader",
            )

        self.assertEqual(result["outcome"], "passed", result)
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
        self.assertTrue(result["temporary_database_removed"])
        self.assertTrue(result["writer_permissions_verified"])

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
            with patch.object(job, "aws_copy", side_effect=responses):
                with self.assertRaises(RuntimeError):
                    job.upload_bundle(
                        config,
                        self.bundle,
                        self.archive,
                        self.receipt,
                    )
            self.assertFalse(marker.exists())

        with patch.object(job, "aws_copy", return_value=None):
            job.upload_bundle(
                config,
                self.bundle,
                self.archive,
                self.receipt,
            )

        self.assertEqual(
            json.loads(marker.read_text()),
            self.receipt,
        )

    def test_uploaded_bundle_keeps_only_its_restorable_archive(self):
        with self.assertRaises(FileNotFoundError):
            job.trim_uploaded_bundle(self.bundle)
        self.assertTrue((self.bundle / "database.dump").exists())

        (self.bundle / "data/raw").mkdir(parents=True)
        (self.bundle / "data/raw/feed.zip").write_bytes(b"x" * 10)
        config = {"BACKUP_BUCKET": "private-bucket", "BACKUP_PREFIX": "election-pipeline"}
        with patch.object(job, "aws_copy", return_value=None):
            job.upload_bundle(config, self.bundle, self.archive, self.receipt)

        self.assertGreaterEqual(job.trim_uploaded_bundle(self.bundle), 10)
        self.assertEqual(
            {entry.name for entry in self.bundle.iterdir()},
            {"bundle.zip", "manifest.json", "upload.json", "uploaded.json"},
        )
        destination = self.root / "download"
        job.unpack_bundle(self.bundle / "bundle.zip", destination, self.receipt)
        self.assertEqual(
            (destination / "database.dump").read_bytes(),
            b"synthetic database archive\x00\xff",
        )


class RemoteRetentionTests(unittest.TestCase):
    """Backups outside the newest five are tagged for the 3-day lifecycle rule."""

    CONFIG = {
        "AWS_PROFILE": "election-backup",
        "AWS_DEFAULT_REGION": "us-east-2",
        "BACKUP_BUCKET": "private-bucket",
        "BACKUP_PREFIX": "election-pipeline",
    }
    NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "s3-retention.json"

    def listing(self, completed, abandoned=()):
        """Backups uploaded hours_ago; abandoned ones lack a completion record."""
        contents = []
        for number, hours_ago in [*completed, *abandoned]:
            backup_id = f"backup_{number:032x}"
            stamp = (self.NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
            names = ["bundle.zip"] + (["complete.json"] if (number, hours_ago) in completed else [])
            for name in names:
                key = f"election-pipeline/rolling/{backup_id}/{name}"
                contents.append({"Key": key, "LastModified": stamp})
        contents.append(
            {
                "Key": "election-pipeline/rolling/not-a-backup/x",
                "LastModified": "2026-11-01T00:00:00Z",
            }
        )
        return {"Contents": contents}

    def run_retention(self, listing, current):
        tagged = []

        def s3api(config, operation, *arguments):
            if operation == "list-objects-v2":
                return listing
            tagged.append(arguments[arguments.index("--key") + 1])
            self.assertIn("TagSet=[{Key=retention,Value=expire}]", arguments)
            return {}

        with patch.object(job, "aws_s3api", side_effect=s3api):
            result = job.mark_expired_backups(
                self.CONFIG, self.state, f"backup_{current:032x}", now=self.NOW
            )
        return result, tagged

    def test_newest_five_are_kept_and_older_backups_are_tagged(self):
        # Eight complete backups six hours apart; number 8 was just uploaded.
        completed = [(n, (8 - n) * 6) for n in range(1, 9)]
        result, tagged = self.run_retention(self.listing(completed), 8)
        self.assertEqual(result["kept"], [f"backup_{n:032x}" for n in (8, 7, 6, 5, 4)])
        self.assertEqual(result["newly_marked"], [f"backup_{n:032x}" for n in (1, 2, 3)])
        self.assertEqual(len(tagged), 6)  # bundle.zip and complete.json of each
        self.assertFalse(any(f"backup_{n:032x}" in key for n in (4, 5, 6, 7, 8) for key in tagged))

    def test_marked_backups_are_not_tagged_again(self):
        completed = [(n, (8 - n) * 6) for n in range(1, 9)]
        self.run_retention(self.listing(completed), 8)
        completed.append((9, 0))
        result, tagged = self.run_retention(self.listing(completed), 9)
        self.assertEqual(result["newly_marked"], [f"backup_{4:032x}"])
        self.assertEqual(len(tagged), 2)

    def test_fewer_than_five_backups_tags_nothing(self):
        result, tagged = self.run_retention(self.listing([(1, 12), (2, 6), (3, 0)]), 3)
        self.assertEqual(tagged, [])
        self.assertEqual(len(result["kept"]), 3)

    def test_abandoned_uploads_are_tagged_only_after_a_day(self):
        listing = self.listing([(1, 0)], abandoned=[(2, 30), (3, 2)])
        result, tagged = self.run_retention(listing, 1)
        self.assertEqual(result["newly_marked"], [f"backup_{2:032x}"])
        self.assertEqual(tagged, [f"election-pipeline/rolling/backup_{2:032x}/bundle.zip"])

    def test_missing_current_upload_stops_before_tagging(self):
        completed = [(n, (8 - n) * 6) for n in range(1, 9)]
        with self.assertRaises(ValueError):
            self.run_retention(self.listing(completed), 99)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
