"""Monitoring contracts: freshness, unchanged sources and notification delivery."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from election_pipeline.settings import Source
from election_pipeline.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "monitor_production", ROOT / "scripts/monitor_production.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        self.recent = (self.now - timedelta(minutes=1)).isoformat()
        self.prod = {
            "DATA_DIR": str(self.root / "data"),
            "PIPELINE_SOURCES": "nc_test,mn_test",
        }
        self.config = {
            "DEPLOYMENT": "election-production",
            "SNS_TOPIC_ARN": "arn:private-test-topic",
            "BACKUP_DIR": str(self.root / "backups"),
        }
        self.sources = {
            "nc_test": Source("nc_test", "NC", "2026-11-03", "https://example.com/nc.zip"),
            "mn_test": Source("mn_test", "MN", "2026-11-03", "https://example.com/mn.zip"),
        }
        for name in self.sources:
            atomic_json(
                self.root / "data/runs" / name / "latest.json",
                {"outcome": "already_current", "finished_at": self.recent},
            )
            atomic_json(
                self.root / "data/state" / (name + ".json"),
                {
                    "last_checked_at": self.recent,
                    "last_changed_at": "2026-10-01T00:00:00+00:00",
                },
            )
        atomic_json(
            self.root / "backups/latest-backup.json",
            {"outcome": "uploaded", "finished_at": self.recent},
        )

    def assess(self, dashboard=None, timer_stopped=False, free=50):
        def unit_state(action, unit):
            if action == "is-active":
                return not (timer_stopped and unit == "election-backup.timer")
            return False

        with (
            patch.object(monitor, "load_config", return_value=(None, self.sources)),
            patch.object(monitor, "systemd", side_effect=unit_state),
            patch.object(
                monitor,
                "dashboard_payload",
                return_value=dashboard
                or {
                    "mode": "published",
                    "races": {"sample": {}},
                    "publication": {"id": "unchanged"},
                },
            ),
            patch.object(
                monitor.shutil,
                "disk_usage",
                return_value=SimpleNamespace(total=100, free=free),
            ),
        ):
            return monitor.assess(self.prod, self.config, self.now)

    def test_two_states_with_unchanged_votes_and_recent_checks_are_healthy(self):
        self.assertEqual(self.assess(), {})

    def test_county_source_failures_alert_without_alerting_uncollected_counties(self):
        self.sources["nc_test"] = replace(self.sources["nc_test"], state="MS")
        payload = {
            "mode": "published",
            "states": {
                "MS": {
                    "availability": "available",
                    "races": {"MS-SEN": {}},
                    "publication": {"id": "partial"},
                    "county_feeds": {
                        "MADISON": {"collection_status": "stale", "issue": "review_required"},
                        "HINDS": {
                            "collection_status": "unavailable",
                            "issue": "source_check_failed",
                        },
                        "PIKE": {"collection_status": "current"},
                        "JACKSON": {"collection_status": "not_collected"},
                    },
                },
                "MN": {
                    "availability": "available",
                    "races": {"MN-SEN": {}},
                    "publication": {"id": "live"},
                },
            },
        }
        issues = self.assess(dashboard=payload)
        self.assertEqual(set(issues), {"county/MS/MADISON", "county/MS/HINDS"})
        self.assertIn("scan review", issues["county/MS/MADISON"])

    def test_manual_county_snapshot_has_separate_freshness_without_http_failure(self):
        self.sources["nc_test"] = replace(self.sources["nc_test"], state="MS")
        record = dict(
            collection_status="stale", issue="manual_snapshot", last_success_at=self.recent
        )
        payload = dict(
            mode="published",
            states={
                "MS": dict(
                    availability="available",
                    races={"MS-SEN": {}},
                    publication={"id": "county"},
                    county_feeds={"JACKSON": record},
                ),
                "MN": dict(
                    availability="available", races={"MN-SEN": {}}, publication={"id": "mn"}
                ),
            },
        )
        self.assertEqual(self.assess(dashboard=payload), {})
        record["last_success_at"] = (self.now - timedelta(hours=7)).isoformat()
        issues = self.assess(dashboard=payload)
        self.assertEqual(set(issues), {"county/MS/JACKSON"})
        self.assertIn("Manual county snapshot", issues["county/MS/JACKSON"])

    def test_catalog_adds_enabled_states_and_ignores_registered_pending_states(self):
        from election_pipeline.deployment import catalog
        from election_pipeline.settings import CollectorSettings

        identity, revision = "sha256:" + "a" * 64, "b" * 40
        value = {
            "version": 1,
            "generation": 0,
            "sources": {},
            "baseline": {
                "source_ids": list(self.sources),
                "image_id": identity,
                "revision": revision,
            },
        }
        for name, source in self.sources.items():
            value["sources"][name] = catalog.entry(
                source,
                CollectorSettings(),
                image_id=identity,
                revision=revision,
                release_path=self.root,
                worker="baseline",
                status="test",
            )
        extra = Source("ga_test", "GA", "2026-11-03", "https://example.com/ga.zip")
        value["sources"]["ga_test"] = catalog.entry(
            extra,
            CollectorSettings(),
            image_id=identity,
            revision=revision,
            release_path=self.root,
            worker="expansion",
            status="test",
        )
        path = self.root / "catalog.json"
        catalog.write(path, value)
        self.prod["IMAGE_TAG"] = revision
        self.config["DEPLOYMENT_CATALOG"] = str(path)
        payload = {
            "mode": "published",
            "states": {
                code: {
                    "availability": "available",
                    "races": {"sample": {}},
                    "publication": {"id": "p"},
                }
                for code in ("NC", "MN")
            },
        }
        with patch(
            "election_pipeline.deployment.containers.inspect_image",
            return_value=(identity, revision),
        ):
            self.assertEqual(self.assess(dashboard=payload), {})
            value["sources"]["ga_test"].update(enabled=True, public=True)
            catalog.write(path, value)
            issues = self.assess(dashboard=payload)
        self.assertIn("pipeline/ga_test", issues)
        self.assertIn("poll/ga_test", issues)
        self.assertIn("dashboard/GA", issues)

    def test_cached_run_success_does_not_hide_stale_official_poll(self):
        atomic_json(
            self.root / "data/state/nc_test.json",
            {"last_checked_at": (self.now - timedelta(hours=1)).isoformat()},
        )
        self.assertIn("poll/nc_test", self.assess())
        self.assertNotIn("pipeline/nc_test", self.assess())

    def test_repeated_failures_and_refusals_are_explained(self):
        failing = {
            "last_checked_at": self.recent,
            "consecutive_failures": 1,
            "last_failure": "HTTP 503",
            "failure_backoff_until": self.recent,
        }
        atomic_json(self.root / "data/state/nc_test.json", failing)
        self.assertNotIn("poll/nc_test", self.assess())  # one transient failure is normal
        atomic_json(self.root / "data/state/nc_test.json", {**failing, "consecutive_failures": 3})
        self.assertIn("3 consecutive runs", self.assess()["poll/nc_test"])
        atomic_json(
            self.root / "data/state/nc_test.json",
            {**failing, "last_failure": "HTTP 403", "blocked_since": self.recent},
        )
        self.assertIn("refused by the site", self.assess()["poll/nc_test"])

    def test_slow_source_poll_interval_does_not_relax_run_monitoring(self):
        self.sources["nc_test"] = replace(self.sources["nc_test"], min_interval_seconds=86400)
        atomic_json(
            self.root / "data/runs/nc_test/latest.json",
            {
                "outcome": "not_due",
                "finished_at": (self.now - timedelta(hours=1)).isoformat(),
            },
        )
        atomic_json(
            self.root / "data/state/nc_test.json",
            {"last_checked_at": (self.now - timedelta(hours=1)).isoformat()},
        )
        issues = self.assess()
        self.assertIn("pipeline/nc_test", issues)
        self.assertNotIn("poll/nc_test", issues)

    def test_timer_backup_dashboard_and_disk_problems_are_all_reported(self):
        atomic_json(
            self.root / "backups/latest-backup.json",
            {
                "outcome": "uploaded",
                # Backups run every six hours; past seven the report is overdue.
                "finished_at": (self.now - timedelta(hours=8)).isoformat(),
            },
        )
        issues = self.assess(
            dashboard={"mode": "demo", "races": {}},
            timer_stopped=True,
            free=5,
        )
        self.assertTrue(
            {"backup", "dashboard", "timer/election-backup.timer", "disk//"} <= set(issues)
        )

    def test_backup_between_scheduled_runs_is_healthy(self):
        atomic_json(
            self.root / "backups/latest-backup.json",
            {"outcome": "uploaded", "finished_at": (self.now - timedelta(hours=6)).isoformat()},
        )
        self.assertNotIn("backup", self.assess())

    def test_failed_s3_retention_is_reported_beside_an_uploaded_backup(self):
        atomic_json(
            self.root / "backups/latest-backup.json",
            {
                "outcome": "uploaded",
                "finished_at": self.recent,
                "s3_retention": {"outcome": "failed", "error": "AccessDenied"},
            },
        )
        issues = self.assess()
        self.assertIn("backup/s3-retention", issues)
        self.assertNotIn("backup", issues)

    def test_alert_deduplication_reminder_and_recovery(self):
        issue = {"backup": "Latest run did not complete successfully"}
        with patch.object(monitor, "publish") as publish:
            state, sent = monitor.notify_changes(self.config, issue, {}, self.now)
            self.assertTrue(sent)
            unchanged, sent = monitor.notify_changes(
                self.config, issue, state, self.now + timedelta(minutes=1)
            )
            self.assertFalse(sent)
            self.assertEqual(unchanged, state)
            state, sent = monitor.notify_changes(
                self.config, issue, state, self.now + timedelta(minutes=31)
            )
            self.assertTrue(sent)
            recovered, sent = monitor.notify_changes(
                self.config, {}, state, self.now + timedelta(minutes=32)
            )
            self.assertTrue(sent)
            _, sent = monitor.notify_changes(
                self.config, {}, recovered, self.now + timedelta(minutes=33)
            )
            self.assertFalse(sent)
        self.assertEqual(publish.call_count, 3)
        self.assertTrue(publish.call_args.args[1].startswith("RECOVERED"))

    def invoke(self, flag=None, publishing_error=None):
        prod_file = self.root / "production.env"
        prod_file.write_text("POSTGRES_PASSWORD=private-password\n")
        config_file = self.root / "monitor.env"
        config_file.write_text("SNS_TOPIC_ARN=arn:private-test-topic\n")
        argv = [
            "monitor_production.py",
            "--env-file",
            str(prod_file),
            "--monitor-env-file",
            str(config_file),
        ] + ([flag] if flag else [])

        with (
            patch.object(sys, "argv", argv),
            patch.object(monitor, "STATE_DIR", self.root / "monitor-state"),
            patch.object(monitor, "assess", return_value={"backup": "failed"}),
            patch.object(monitor, "publish", side_effect=publishing_error) as publish,
            patch.object(monitor, "heartbeat") as heartbeat,
            patch.object(monitor.os, "umask"),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = monitor.main()
        return code, publish, heartbeat, output.getvalue()

    def test_dry_run_sends_nothing_and_does_not_write_state(self):
        code, publish, heartbeat, _ = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        publish.assert_not_called()
        heartbeat.assert_not_called()
        self.assertFalse((self.root / "monitor-state").exists())

    def test_test_alert_does_not_emit_a_fake_heartbeat(self):
        code, publish, heartbeat, _ = self.invoke("--test-alert")
        self.assertEqual(code, 0)
        publish.assert_called_once()
        heartbeat.assert_not_called()

    def test_failed_notification_is_not_saved_as_sent_and_no_heartbeat_is_emitted(self):
        code, _, heartbeat, output = self.invoke(
            publishing_error=RuntimeError("private-password arn:private-test-topic denied")
        )
        self.assertEqual(code, 1)
        heartbeat.assert_not_called()
        self.assertFalse((self.root / "monitor-state/state.json").exists())
        self.assertNotIn("private-password", output)
        self.assertNotIn("arn:private-test-topic", output)
        self.assertEqual(json.loads(output)["outcome"], "monitor_failed")


if __name__ == "__main__":
    unittest.main()
