"""One go-live command: every step in order, each gate in code, resumable."""

import contextlib
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from election_pipeline.deployment import catalog, control, golive, manual

NAME = "ut_2026_general"  # acquired over http
MANUAL = "oh_2026_general"  # declared manual
PUBLICATION = "pub-ut"
CANDIDATE = {"id": "c" * 32, "url": "http://127.0.0.1:8001", "sources": [NAME]}


class GoLiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.config = {
            "CONTROL_STATE_DIR": str(self.root),
            "BACKUP_DIR": str(self.backups),
            "GO_LIVE_RESTORE_WAIT_SECONDS": "3600",
        }
        self.value = {
            "generation": 0,
            "release": {
                "release_path": str(self.root / "release"),
                "image_id": "sha256:" + "a" * 64,
            },
            "sources": {},
        }
        self.name = NAME  # the source under test; a manual one for the manual path
        self.calls, self.clock = [], 0.0
        self.fail = set()
        self.check_runs = True  # the backup's follow-on check runs and verifies
        self.own_check_verifies = True

    # The catalog is held in memory (its file lock is a Linux host's).

    def read(self, path=None):
        return copy.deepcopy(self.value)

    def source(self):
        return self.value["sources"][self.name]

    def called(self, name, **changes):
        def fake(*args, **kwargs):
            self.calls.append(name)
            if name in self.fail:
                self.fail.discard(name)
                raise ValueError(f"{name} refused")
            if changes:
                self.source().update(changes)
            return {"outcome": name}

        return fake

    def report(self, finished=None):
        finished = finished or datetime.now(timezone.utc)
        (self.backups / "latest-check-restore.json").write_text(
            json.dumps(
                {
                    "outcome": "passed",
                    "temporary_database_removed": True,
                    "writer_permissions_verified": True,
                    "reader_permissions_verified": True,
                    "finished_at": finished.isoformat(),
                    "current_publications": {self.name: PUBLICATION},
                    "database_inventory": {"serving.contest_results": {"rows": 5}},
                }
            ),
            encoding="utf-8",
        )

    def command(self, args, timeout=None):
        if args[:2] == ["systemctl", "start"]:
            self.calls.append("backup")
            if "backup" in self.fail:
                self.fail.discard("backup")
                raise RuntimeError("Command failed (systemctl, exit 1)")
            if self.check_runs:
                self.report(datetime.now(timezone.utc))
            return ""
        if args[:2] == ["systemctl", "show"]:
            return "inactive"
        self.calls.append("check-restore")
        if self.own_check_verifies:
            self.report(datetime.now(timezone.utc))
        return ""

    def sleep(self, seconds):
        self.clock += seconds

    def go(self, **options):
        fakes = {
            "register": self.called("register", acquisition="http", enabled=False, public=False),
            "prepare": self.called("prepare", candidate_id="candidate-ut"),
            "promote": self.called(
                "promote",
                publication_id=PUBLICATION,
                published_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            ),
            "start_dashboard": self.called("start_dashboard"),
            "activate": self.called("activate", enabled=True, public=True),
        }

        def register(path, name):
            self.value["sources"][name] = {"status": "unofficial"}
            return fakes["register"]()

        def start(path, names, prod, config):
            fakes["start_dashboard"]()
            return dict(CANDIDATE)

        def activate(path, candidate, report, config):
            self.assertTrue(report["current_publications"][self.name])
            return fakes["activate"]()

        def prepare(path, name, prod, config):
            result = fakes["prepare"]()
            self.source().pop("candidate_id", None)
            self.source()["candidate_id"] = "candidate-ut"
            return result

        def promote(path, name, prod, config):
            result = fakes["promote"]()
            self.source().pop("candidate_id", None)
            return result

        with (
            patch.object(catalog, "read", side_effect=self.read),
            patch.object(catalog, "locked", side_effect=lambda *a, **k: contextlib.nullcontext()),
            patch.object(control, "register", side_effect=register),
            patch.object(control, "prepare", side_effect=prepare),
            patch.object(control, "promote", side_effect=promote),
            patch.object(control, "start_dashboard", side_effect=start),
            patch.object(control, "activate", side_effect=activate),
        ):
            return golive.go_live(
                self.root / "catalog.json",
                self.name,
                {"POSTGRES_PASSWORD": "secret"},
                self.config,
                run=self.command,
                sleep=self.sleep,
                monotonic=lambda: self.clock,
                **options,
            )

    def journal(self):
        return json.loads((self.root / f"golive-{self.name}.json").read_text(encoding="utf-8"))

    def test_an_http_source_runs_every_step_in_order_and_is_journaled(self):
        result = self.go()
        self.assertEqual(result["outcome"], "live")
        self.assertEqual(
            self.calls,
            ["register", "prepare", "promote", "backup", "start_dashboard", "activate"],
        )
        steps = self.journal()["steps"]
        self.assertEqual(
            {k: v["outcome"] for k, v in steps.items()},
            {
                "register": "done",
                "configure": "skipped",
                "acquire": "done",
                "promote": "done",
                "backup": "done",
                "restore_check": "done",
                "candidate": "done",
                "activate": "done",
            },
        )
        self.assertEqual(self.journal()["outcome"], "done")
        # A public source has nothing left to do.
        self.calls.clear()
        self.assertEqual(self.go()["outcome"], "already_public")
        self.assertEqual(self.calls, [])

    def test_a_stopped_run_resumes_without_repeating_finished_steps(self):
        self.fail.add("start_dashboard")
        with self.assertRaisesRegex(ValueError, "stopped at candidate: start_dashboard refused"):
            self.go()
        self.assertEqual(self.journal()["outcome"], "failed")
        self.assertEqual(self.journal()["steps"]["candidate"]["outcome"], "failed")
        self.assertEqual(self.journal()["steps"]["promote"]["outcome"], "done")
        self.calls.clear()
        # The restore check that verified the publication still holds: no second backup.
        self.assertEqual(self.go()["outcome"], "live")
        self.assertEqual(self.calls, ["start_dashboard", "activate"])
        self.assertEqual(self.journal()["steps"]["backup"]["outcome"], "skipped")

    def test_a_failed_prepare_stops_before_anything_is_promoted_or_backed_up(self):
        self.fail.add("prepare")
        with self.assertRaisesRegex(ValueError, "stopped at acquire"):
            self.go()
        self.assertEqual(self.calls, ["register", "prepare"])
        self.calls.clear()
        self.assertEqual(self.go()["outcome"], "live")
        self.assertEqual(self.calls[0], "prepare")
        self.assertNotIn("register", self.calls)

    def test_nothing_activates_without_a_restore_check_that_verified_the_publication(self):
        self.check_runs = False
        self.own_check_verifies = False
        with self.assertRaisesRegex(ValueError, "did not verify this publication"):
            self.go()
        self.assertEqual(self.calls.count("check-restore"), 1)
        self.assertNotIn("activate", self.calls)
        self.assertEqual(self.journal()["steps"]["restore_check"]["outcome"], "failed")
        self.assertFalse(self.source().get("public"))

    def test_a_check_that_does_not_start_after_the_backup_is_run_by_the_command(self):
        self.check_runs = False
        self.assertEqual(self.go()["outcome"], "live")
        self.assertEqual(self.calls.count("check-restore"), 1)
        self.assertGreaterEqual(self.clock, golive.START_GRACE_SECONDS)

    def test_a_restore_check_from_before_the_publication_does_not_count(self):
        self.check_runs = False
        self.own_check_verifies = False
        self.report(datetime.now(timezone.utc) - timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "did not verify"):
            self.go()
        self.assertNotIn("activate", self.calls)

    def test_a_failed_backup_stops_the_run(self):
        self.fail.add("backup")
        with self.assertRaisesRegex(ValueError, "stopped at backup"):
            self.go()
        self.assertNotIn("start_dashboard", self.calls)

    def test_a_manual_source_is_configured_and_imported_from_the_operators_file(self):
        self.name = MANUAL  # declared manual in the election definition
        acquire = {"file": self.root / "inbox/f.txt", "sha256": "1" * 64, "downloaded_at": "t"}

        def configure(path, name, **options):
            self.calls.append("configure")
            self.source().update(acquisition="manual")

        def operate(path, name, prod, config, **options):
            self.calls.append("import")
            self.assertEqual(options, acquire)
            self.source().update(candidate_id="candidate-ut", manual_candidate={})

        with (
            patch.object(manual, "configure", side_effect=configure),
            patch.object(manual, "operate", side_effect=operate),
        ):
            self.assertEqual(self.go(acquire=acquire)["outcome"], "live")
        self.assertEqual(self.calls[:4], ["register", "configure", "import", "promote"], self.calls)
        self.assertNotIn("prepare", self.calls)

    def test_a_file_for_a_source_the_definition_acquires_over_http_is_refused(self):
        acquire = {"file": self.root / "inbox/f.txt", "sha256": "1" * 64, "downloaded_at": "t"}
        with self.assertRaisesRegex(ValueError, "acquires ut_2026_general over http"):
            self.go(acquire=acquire)
        self.assertEqual(self.calls, [])

    def test_a_manual_source_without_its_file_is_refused_before_it_is_imported(self):
        self.name = MANUAL
        self.value["sources"][self.name] = {
            "acquisition": "manual",
            "enabled": False,
            "public": False,
            "status": "unofficial",
        }
        with self.assertRaisesRegex(ValueError, "needs its file"):
            self.go()
        self.assertEqual(self.calls, [])

    def test_the_operator_can_look_at_the_checked_candidate_before_it_goes_public(self):
        result = self.go(stop_before_activation=True)
        self.assertEqual(result["outcome"], "stopped_before_activation")
        self.assertEqual(result["candidate"], CANDIDATE["id"])
        self.assertNotIn("activate", self.calls)
        self.assertEqual(self.journal()["outcome"], "stopped")
        self.calls.clear()
        self.assertEqual(self.go()["outcome"], "live")
        self.assertEqual(self.calls, ["start_dashboard", "activate"])

    def test_errors_are_reported_without_secrets(self):
        def refuse(path, name, prod, config):
            raise ValueError("connect failed for password secret")

        with (
            patch.object(catalog, "read", side_effect=self.read),
            patch.object(catalog, "locked", side_effect=lambda *a, **k: contextlib.nullcontext()),
            patch.object(control, "register", side_effect=refuse),
        ):
            with self.assertRaises(ValueError) as caught:
                golive.go_live(
                    self.root / "catalog.json",
                    self.name,
                    {"POSTGRES_PASSWORD": "secret"},
                    self.config,
                    run=self.command,
                    sleep=self.sleep,
                    monotonic=lambda: self.clock,
                )
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", json.dumps(self.journal()))


if __name__ == "__main__":
    unittest.main()
