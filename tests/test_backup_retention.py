"""Local retention preserves recent, incomplete and unrelated data."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prune_restore_work

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "prune_backups",
    ROOT / "scripts/prune_backups.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundles = []

        for number in range(1, 6):
            name = f"backup_{number:032x}"
            bundle = self.root / name
            bundle.mkdir()
            (bundle / "uploaded.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "backup_id": name,
                        "sha256": "a" * 64,
                        "created_at": (f"2026-11-02T0{number}:00:00+00:00"),
                    }
                )
            )
            self.bundles.append(bundle)

    def test_dry_run_changes_nothing(self):
        result = module.prune_backups(self.root)
        self.assertEqual(result["outcome"], "planned")
        self.assertEqual(len(result["would_remove"]), 2)
        self.assertTrue(all(bundle.exists() for bundle in self.bundles))

    def test_apply_preserves_newest_and_unmarked_data(self):
        incomplete = self.root / ("backup_" + "f" * 32)
        incomplete.mkdir()
        restored = self.root / "restore-proof"
        restored.mkdir()

        result = module.prune_backups(self.root, keep=3, apply=True)

        self.assertEqual(
            result["removed"],
            [
                self.bundles[1].name,
                self.bundles[0].name,
            ],
        )
        self.assertFalse(self.bundles[0].exists())
        self.assertFalse(self.bundles[1].exists())
        self.assertTrue(all(bundle.exists() for bundle in self.bundles[2:]))
        self.assertTrue(incomplete.exists())
        self.assertTrue(restored.exists())

    def test_invalid_marker_stops_before_deletion(self):
        marker = self.bundles[0] / "uploaded.json"
        receipt = json.loads(marker.read_text())
        receipt["backup_id"] = "wrong"
        marker.write_text(json.dumps(receipt))

        with self.assertRaises(ValueError):
            module.prune_backups(self.root, apply=True)

        self.assertTrue(all(bundle.exists() for bundle in self.bundles))

    def test_zero_retention_is_rejected(self):
        with self.assertRaises(ValueError):
            module.prune_backups(self.root, keep=0, apply=True)
        self.assertTrue(all(bundle.exists() for bundle in self.bundles))

    def test_restore_pruning_requires_a_passed_report_and_a_matching_remote_backup(self):
        report_dir = self.root / "reports"
        report_dir.mkdir()
        receipt = {
            "schema_version": 1,
            "backup_id": self.bundles[0].name,
            "sha256": "a" * 64,
            "bytes": 100,
        }
        for index, passed in enumerate((True, True, False)):
            work = self.root / f"restore-{index:08}"
            (work / "bundle").mkdir(parents=True)
            local = {**receipt, "backup_id": self.bundles[index].name}
            (work / "complete.json").write_text(json.dumps(local))
            (report_dir / f"election_restore_{index}_test.json").write_text(
                json.dumps(
                    {
                        "outcome": "passed" if passed else "failed",
                        "temporary_database_removed": True,
                        "backup_dir": str(work / "bundle"),
                    }
                )
            )
        config = {"BACKUP_BUCKET": "private-fixture"}
        responses = [json.dumps(receipt), "100", json.dumps(receipt)]
        with patch.object(prune_restore_work.subprocess, "check_output", side_effect=responses):
            plan = prune_restore_work.prune(self.root, report_dir, config)
        self.assertEqual(plan["would_remove"], ["restore-00000000"])
        self.assertTrue((self.root / "restore-00000000").exists())
        with patch.object(prune_restore_work.subprocess, "check_output", side_effect=responses):
            result = prune_restore_work.prune(self.root, report_dir, config, apply=True)
        self.assertEqual(result["removed"], ["restore-00000000"])
        self.assertFalse((self.root / "restore-00000000").exists())
        self.assertTrue((self.root / "restore-00000001").exists())
        self.assertTrue((self.root / "restore-00000002").exists())
        self.assertEqual(len(list(report_dir.iterdir())), 3)
        self.assertTrue(all(bundle.exists() for bundle in self.bundles))

    def test_link_cannot_remove_outside_directory(self):
        with tempfile.TemporaryDirectory() as outside:
            proof = Path(outside) / "preserve.txt"
            proof.write_text("outside")
            link = self.root / ("backup_" + "f" * 32)

            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is unavailable")

            with self.assertRaises(ValueError):
                module.prune_backups(self.root, apply=True)

            self.assertEqual(proof.read_text(), "outside")
            self.assertTrue(all(bundle.exists() for bundle in self.bundles))


if __name__ == "__main__":
    unittest.main()
