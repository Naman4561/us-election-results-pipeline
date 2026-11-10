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

from election_pipeline.settings import load_config
from election_pipeline.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "monitor_production", ROOT / "scripts/monitor_production.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


NC, MN, MS = "nc_2026_general_all", "mn_2026_general_all", "ms_2026_general_all"
IMAGE, REVISION = "sha256:" + "a" * 64, "b" * 40


def payload(*codes, **states):
    return {
        "mode": "published",
        "states": {
            **{
                code: {"availability": "available", "races": {"x": {}}, "publication": {"id": code}}
                for code in codes
            },
            **states,
        },
    }


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
        self.recent = (self.now - timedelta(minutes=1)).isoformat()
        self.prod = {"DATA_DIR": str(self.root / "data")}
        self.config = {
            "DEPLOYMENT": "election-production",
            "SNS_TOPIC_ARN": "arn:private-test-topic",
            "BACKUP_DIR": str(self.root / "backups"),
            "CATALOG_FILE": str(self.root / "catalog.json"),
        }
        _, configured = load_config(ROOT / "config/sources.toml")
        self.sources = {name: configured[name] for name in (NC, MN, MS)}
        self.catalog = {
            "version": 2,
            "generation": 1,
            "release": {"revision": REVISION, "image_id": IMAGE, "release_path": str(self.root)},
            "previous_release": None,
            "dashboard": {"name": "election-dashboard-x", "url": "u", "states": ["MN", "NC"]},
            "sources": {
                name: {
                    "enabled": name != MS,
                    "public": name != MS,
                    "status": "test",
                    "acquisition": "http",
                }
                for name in self.sources
            },
        }
        for name in self.sources:
            self.healthy(name)
        atomic_json(
            self.root / "backups/latest-backup.json",
            {"outcome": "uploaded", "finished_at": self.recent},
        )

    def healthy(self, name):
        atomic_json(
            self.root / "data/runs" / name / "latest.json",
            {"outcome": "already_current", "finished_at": self.recent},
        )
        atomic_json(
            self.root / "data/state" / (name + ".json"),
            {"last_checked_at": self.recent, "last_changed_at": "2026-10-01T00:00:00+00:00"},
        )

    def publish(self, name, public=True):
        self.catalog["sources"][name].update(enabled=True, public=public)

    def assess(
        self, dashboard=None, timer_stopped=False, free=50, image=(IMAGE, REVISION), failed=()
    ):
        from election_pipeline.deployment import catalog

        def unit_state(action, unit):
            if action == "is-active":
                return not (timer_stopped and unit == "election-backup.timer")
            return unit in failed

        with patch.object(catalog, "configured", return_value=self.sources):
            catalog.write(self.config["CATALOG_FILE"], self.catalog)
            with (
                patch.object(monitor, "systemd", side_effect=unit_state),
                patch("election_pipeline.deployment.containers.inspect_image", return_value=image),
                patch.object(
                    monitor, "dashboard_payload", return_value=dashboard or payload("NC", "MN")
                ),
                patch.object(
                    monitor.shutil, "disk_usage", return_value=SimpleNamespace(total=100, free=free)
                ),
            ):
                return monitor.assess(self.prod, self.config, self.now)

    def test_two_states_with_unchanged_votes_and_recent_checks_are_healthy(self):
        self.assertEqual(self.assess(), {})

    def test_county_source_failures_alert_without_alerting_uncollected_counties(self):
        self.catalog["sources"][NC].update(enabled=False, public=False)
        self.publish(MS)
        feeds = {
            "MADISON": {"collection_status": "stale", "issue": "review_required"},
            "HINDS": {"collection_status": "unavailable", "issue": "source_check_failed"},
            "PIKE": {"collection_status": "current"},
            "JACKSON": {"collection_status": "not_collected"},
        }
        ms = {"availability": "available", "races": {"MS-SEN": {}}, "publication": {"id": "p"}}
        issues = self.assess(dashboard=payload("MN", MS={**ms, "county_feeds": feeds}))
        self.assertEqual(set(issues), {"county/MS/MADISON", "county/MS/HINDS"})
        self.assertIn("scan review", issues["county/MS/MADISON"])

    def test_manual_county_snapshot_has_separate_freshness_without_http_failure(self):
        self.catalog["sources"][NC].update(enabled=False, public=False)
        self.publish(MS)
        record = dict(
            collection_status="stale", issue="manual_snapshot", last_success_at=self.recent
        )
        ms = dict(
            availability="available",
            races={"MS-SEN": {}},
            publication={"id": "county"},
            county_feeds={"JACKSON": record},
        )
        self.assertEqual(self.assess(dashboard=payload("MN", MS=ms)), {})
        record["last_success_at"] = (self.now - timedelta(hours=7)).isoformat()
        issues = self.assess(dashboard=payload("MN", MS=ms))
        self.assertEqual(set(issues), {"reminder/county/MS/JACKSON"})
        alerts, reminders = monitor.split(issues)
        self.assertEqual(alerts, {})
        self.assertIn("Manual county snapshot", reminders["county/MS/JACKSON"])

    def test_enabling_a_source_adds_its_checks_and_private_ones_stay_off_the_dashboard(self):
        (self.root / "data/runs" / MS / "latest.json").unlink()
        (self.root / "data/state" / (MS + ".json")).unlink()
        self.assertEqual(self.assess(), {})
        self.publish(MS, public=False)
        issues = self.assess()
        self.assertIn("pipeline/" + MS, issues)
        self.assertIn("poll/" + MS, issues)
        self.assertNotIn("dashboard/MS", issues)
        self.publish(MS)
        self.assertIn("dashboard/coverage", self.assess())

    def test_a_paused_source_is_a_reminder_but_its_publication_is_still_checked(self):
        self.catalog["sources"][NC]["paused"] = True
        stale = (self.now - timedelta(days=2)).isoformat()
        atomic_json(
            self.root / "data/runs" / NC / "latest.json",
            {"outcome": "failed", "finished_at": stale},
        )
        atomic_json(self.root / "data/state" / (NC + ".json"), {"last_checked_at": stale})
        issues = self.assess()
        self.assertEqual(set(issues), {monitor.REMINDER + "paused/" + NC})
        self.assertIn("dashboard/NC", self.assess(dashboard=payload("MN")))

    def test_an_official_feed_is_a_reminder_until_certified_and_then_needs_none(self):
        atomic_json(
            self.root / "data/runs" / NC / "latest.json",
            {
                "outcome": "already_current",
                "finished_at": self.recent,
                "stages": [{"stage": "parse", "result": {"official_results": True}}],
            },
        )
        self.assertEqual(set(self.assess()), {monitor.REMINDER + "official/" + NC})
        # Certified: official and paused for good, with nothing left to remind about.
        self.catalog["sources"][NC].update(status="official", paused=True)
        self.assertEqual(self.assess(), {})

    def test_release_image_and_current_symlink_must_match_the_catalog(self):
        self.assertIn("deployment/release", self.assess(image=(IMAGE, "c" * 40)))
        self.config["CURRENT_RELEASE"] = str(self.root / "elsewhere")
        self.assertIn("deployment/current", self.assess())

    def test_cached_run_success_does_not_hide_stale_official_poll(self):
        atomic_json(
            self.root / "data/state" / (NC + ".json"),
            {"last_checked_at": (self.now - timedelta(hours=2)).isoformat()},
        )
        self.assertIn("poll/" + NC, self.assess())
        self.assertNotIn("pipeline/" + NC, self.assess())

    def test_repeated_failures_and_refusals_are_explained(self):
        failing = {
            "last_checked_at": self.recent,
            "consecutive_failures": 1,
            "last_failure": "HTTP 503",
            "failure_backoff_until": self.recent,
        }
        atomic_json(self.root / "data/state" / (NC + ".json"), failing)
        self.assertNotIn("poll/" + NC, self.assess())  # one transient failure is normal
        atomic_json(
            self.root / "data/state" / (NC + ".json"), {**failing, "consecutive_failures": 3}
        )
        self.assertIn("3 consecutive runs", self.assess()["poll/" + NC])
        atomic_json(
            self.root / "data/state" / (NC + ".json"),
            {**failing, "last_failure": "HTTP 403", "blocked_since": self.recent},
        )
        self.assertIn("refused by the site", self.assess()["poll/" + NC])

    def test_a_failed_run_says_where_and_why_and_a_failed_service_points_at_it(self):
        atomic_json(
            self.root / "data/runs" / NC / "latest.json",
            {
                "outcome": "failed",
                "finished_at": self.recent,
                "error": "Parsing failed: County and official contest\n candidate totals disagree",
                "stages": [
                    {"stage": "collect", "outcome": "not_due"},
                    {"stage": "parse", "outcome": "validation_failed"},
                ],
            },
        )
        atomic_json(
            self.root / "backups/latest-backup.json",
            {"outcome": "failed", "finished_at": self.recent, "error": "S3 transfer failed"},
        )
        issues = self.assess(failed=("election-worker.service", "election-backup.service"))
        self.assertEqual(
            issues["pipeline/" + NC],
            "Latest run failed at parse (validation_failed): "
            "Parsing failed: County and official contest candidate totals disagree",
        )
        self.assertEqual(issues["backup"], "Latest run failed: S3 transfer failed")
        self.assertEqual(
            issues["service/election-worker.service"],
            "Service is in a failed state; see pipeline/" + NC,
        )
        self.assertEqual(
            issues["service/election-backup.service"], "Service is in a failed state; see backup"
        )

    def test_slow_source_poll_interval_does_not_relax_run_monitoring(self):
        self.sources[NC] = replace(self.sources[NC], min_interval_seconds=86400)
        atomic_json(
            self.root / "data/runs" / NC / "latest.json",
            {
                "outcome": "not_due",
                "finished_at": (self.now - timedelta(hours=2)).isoformat(),
            },
        )
        atomic_json(
            self.root / "data/state" / (NC + ".json"),
            {"last_checked_at": (self.now - timedelta(hours=2)).isoformat()},
        )
        issues = self.assess()
        self.assertIn("pipeline/" + NC, issues)
        self.assertNotIn("poll/" + NC, issues)

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

    def test_manual_reminders_are_sent_apart_from_alerts_and_repeat_every_six_hours(self):
        reminders = {"manual/oh_2026_general": "Manual snapshot is overdue for an operator update"}
        with patch.object(monitor, "publish") as publish:
            state, sent = monitor.notify_reminders(self.config, reminders, {}, self.now)
            self.assertTrue(sent)
            self.assertTrue(publish.call_args.args[1].startswith("REMINDER"))
            later = self.now + timedelta(hours=1)
            state, sent = monitor.notify_reminders(self.config, reminders, state, later)
            self.assertFalse(sent)
            more = {
                **reminders,
                "county/MS/JACKSON": "Manual county snapshot is over six hours old",
            }
            state, sent = monitor.notify_reminders(self.config, more, state, later)
            self.assertTrue(sent)  # a new reminder is sent at once
            state, sent = monitor.notify_reminders(
                self.config, more, state, later + timedelta(hours=5)
            )
            self.assertFalse(sent)
            state, sent = monitor.notify_reminders(
                self.config, more, state, later + timedelta(hours=6)
            )
            self.assertTrue(sent)
            cleared, sent = monitor.notify_reminders(
                self.config, {}, state, later + timedelta(hours=7)
            )
            self.assertFalse(sent)  # no "recovered" message for operator tasks
            self.assertEqual(cleared, {})
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(
            monitor.split({"reminder/manual/x": "late", "backup": "bad"}),
            (
                {"backup": "bad"},
                {"manual/x": "late"},
            ),
        )

    def invoke(self, flag=None, publishing_error=None):
        prod_file = self.root / "production.env"
        prod_file.write_text("POSTGRES_PASSWORD=private-password\n")
        config_file = self.root / "monitor.env"
        config_file.write_text("SNS_TOPIC_ARN=arn:private-test-topic\n")
        deployment_file = self.root / "deployment.env"
        deployment_file.write_text("CATALOG_FILE=" + self.config["CATALOG_FILE"] + "\n")
        argv = [
            "monitor_production.py",
            "--env-file",
            str(prod_file),
            "--monitor-env-file",
            str(config_file),
            "--deployment-env-file",
            str(deployment_file),
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
