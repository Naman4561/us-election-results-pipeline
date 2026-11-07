"""Retention planning for re-creatable working files."""

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from election_pipeline.settings import Source
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prune_data", ROOT / "scripts/prune_data.py")
prune = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune)

CURRENT = "a" * 64
OLD_SHA = "b" * 64
ATTEMPT = "12345678-1234-1234-1234-123456789abc"


class PruneDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.source = Source("nc_test", "NC", "2026-11-03", "https://example.com/nc.zip")
        self.sources = {"nc_test": self.source}
        self.now = time.time()
        ArchiveStore(self.data, self.source).write_state({"sha256": CURRENT})
        self.files = {
            "raw": self.write(f"raw/NC/2026-11-03/nc_test/{OLD_SHA}.zip"),
            "metadata": self.write("metadata/nc_test/2026-11-01/abc.json"),
            "quarantine": self.write("quarantine/nc_test/x.bin"),
            "latest": self.write("runs/nc_test/latest.json"),
            "run": self.write("runs/nc_test/0123.json"),
            "fresh_run": self.write("runs/nc_test/4567.json", age_days=1),
            "report": self.write("reports/parsing/nc_test/f.json"),
            "current_parsed": self.write(f"parsed/nc_test/{CURRENT[:16]}-{'1' * 16}/report.json"),
            "old_parsed": self.write(f"parsed/nc_test/{OLD_SHA[:16]}-{'2' * 16}/report.json"),
            "artifacts": self.write(f"publications/{ATTEMPT}/manifest.json"),
            "unknown": self.write("publications/not-an-attempt/manifest.json"),
        }

    def write(self, relative, age_days=30):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        stamp = self.now - age_days * 86400
        for item in (path, path.parent):
            os.utime(item, (stamp, stamp))
        return path

    def run_prune(self, apply):
        return prune.prune_data(self.data, self.sources, keep_days=7, apply=apply, now=self.now)

    def test_dry_run_plans_only_recreatable_old_files(self):
        result = self.run_prune(apply=False)
        planned = {Path(path).as_posix() for path in result["removed"]}
        self.assertEqual(
            planned,
            {
                "runs/nc_test/0123.json",
                "reports/parsing/nc_test/f.json",
                f"parsed/nc_test/{OLD_SHA[:16]}-{'2' * 16}",
                f"publications/{ATTEMPT}",
            },
        )
        self.assertTrue(all(path.exists() for path in self.files.values()))

    def test_apply_removes_planned_files_and_keeps_evidence(self):
        self.run_prune(apply=True)
        for name in ("run", "report", "old_parsed", "artifacts"):
            self.assertFalse(self.files[name].exists(), name)
        for name in ("raw", "metadata", "quarantine", "latest", "fresh_run", "current_parsed"):
            self.assertTrue(self.files[name].exists(), name)
        self.assertTrue(self.files["unknown"].exists())

    def test_locked_source_is_skipped_and_unreadable_state_keeps_parsed_outputs(self):
        with ArchiveStore(self.data, self.source).lock(stage="pipeline"):
            result = self.run_prune(apply=True)
        self.assertEqual(result["skipped_locked"], ["nc_test"])
        self.assertTrue(self.files["run"].exists())
        (self.data / "state/nc_test.json").write_text(json.dumps({"schema_version": 2}))
        self.run_prune(apply=True)
        self.assertTrue(self.files["old_parsed"].exists())
        self.assertFalse(self.files["run"].exists())

    def test_rejects_zero_retention(self):
        with self.assertRaises(ValueError):
            prune.prune_data(self.data, self.sources, keep_days=0)


if __name__ == "__main__":
    unittest.main()
