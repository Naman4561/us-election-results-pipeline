"""One release for every source: catalog rules, the worker, release switches and routing."""

import copy
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from election_pipeline import recovery
from election_pipeline.dashboard.server import create_app
from election_pipeline.deployment import catalog, containers, control, dashboard
from election_pipeline.workflow import NO_SNAPSHOT

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64
REVISION = "b" * 40
NEW_IMAGE = "sha256:" + "c" * 64
NEW_REVISION = "d" * 40
NC, KY, GA, SC = "nc_2026_general_all", "ky_2026_general", "ga_2026_general", "sc_2026_general"


def fixture(root):
    """NC and KY are public; GA and SC are configured but not yet enabled."""

    def source(public):
        return {"enabled": public, "public": public, "status": "test", "acquisition": "http"}

    return {
        "version": 2,
        "generation": 0,
        "release": {"revision": REVISION, "image_id": IMAGE, "release_path": str(root)},
        "previous_release": None,
        "dashboard": {
            "name": "election-dashboard-" + "0" * 32,
            "url": "http://old",
            "states": ["KY", "NC"],
        },
        "sources": {NC: source(True), KY: source(True), GA: source(False), SC: source(False)},
    }


spec = importlib.util.spec_from_file_location("cutover", ROOT / "scripts/cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        self.value = fixture(self.root)
        catalog.write(self.path, self.value)
        self.prod = {
            "DATA_DIR": str(self.root / "data"),
            "POSTGRES_DB": "elections",
            "POSTGRES_USER": "admin",
            "POSTGRES_PASSWORD": "administrator-secret",
            "PIPELINE_DB_USER": "writer",
            "PIPELINE_DB_PASSWORD": "writer-secret",
            "DASHBOARD_DB_USER": "reader",
            "DASHBOARD_DB_PASSWORD": "reader-secret",
        }
        self.config = {
            "JOB_CPUS": "0.5",
            "JOB_MEMORY": "768m",
            "CONTROL_STATE_DIR": str(self.root),
            "CADDYFILE": str(self.root / "Caddyfile"),
            "CADDY_CONTAINER": "caddy-id",
            "CATALOG_FILE": str(self.path),
            "CURRENT_RELEASE": str(self.root / "current"),
        }

    def save(self):
        catalog.write(self.path, self.value)

    # Catalog --------------------------------------------------------------------------

    def test_catalog_accepts_only_configured_sources_and_consistent_flags(self):
        self.assertEqual(catalog.read(self.path), self.value)
        self.assertEqual(catalog.public_states(self.value), {"NC", "KY"})
        for change in (
            lambda v: v["sources"].update(az_2026_general=dict(v["sources"][GA])),
            lambda v: v["sources"][GA].update(public=True),
            lambda v: v["sources"][GA].update(acquisition="ftp"),
            # Only a collected source can be paused, and only with a boolean.
            lambda v: v["sources"][GA].update(paused=True),
            lambda v: v["sources"][NC].update(paused="yes"),
            lambda v: v["sources"][NC].update(status="certified"),
            # Ancillary datasets are collected by hand, never by the worker.
            lambda v: v["sources"].update(nc_absentee_early_demographics_2026={**v["sources"][NC]}),
            lambda v: v["release"].update(image_id="election-pipeline:latest"),
            lambda v: v.update(version=1),
        ):
            value = copy.deepcopy(self.value)
            change(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                catalog.validate(value)

    def test_dashboard_filters_private_states_and_fails_closed_on_invalid_catalog(self):
        providers = {"NC": lambda: {"publication": None}, "KY": lambda: {"publication": None}}
        client = TestClient(create_app(state_providers=providers, catalog_file=self.path))
        result = client.get("/api/national")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json()["coverage"]), {"NC", "KY"})
        self.assertEqual(client.get("/api/states/GA").status_code, 404)
        self.path.write_text("invalid json", encoding="utf-8")
        self.assertEqual(client.get("/api/national").status_code, 503)
        self.assertEqual(client.get("/api/states/NC").status_code, 503)

    # Worker ---------------------------------------------------------------------------

    def test_job_timeout_removes_only_its_owned_container_and_keeps_secrets_out_of_argv(self):
        with patch.object(containers.subprocess, "run") as run:
            run.side_effect = [
                subprocess.TimeoutExpired("docker", 2),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                containers.run_job(
                    IMAGE, self.prod, self.config, ["scripts/run_source.py"], timeout=2
                )
        first = run.call_args_list[0]
        args = first.args[0]
        self.assertNotIn("writer-secret", str(args))
        self.assertEqual(first.kwargs["env"]["POSTGRES_PASSWORD"], "writer-secret")
        self.assertIn(IMAGE, args)
        for flag in ("--memory", "--cpus", "--read-only"):
            self.assertIn(flag, args)
        self.assertEqual(run.call_args_list[1].args[0][-1], args[args.index("--name") + 1])
        self.assertNotIn("administrator-secret", str(first))

    def test_missing_resource_budget_fails_before_starting_a_container(self):
        with patch.object(containers, "command") as command:
            with self.assertRaises(ValueError):
                containers.run_job(IMAGE, self.prod, {}, [])
        command.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_one_failed_source_does_not_stop_the_others_and_secrets_are_redacted(self):
        with patch.object(
            control,
            "run_one",
            side_effect=lambda image, name, *a, **k: (
                (_ for _ in ()).throw(RuntimeError("writer-secret failed"))
                if name == NC
                else {"source_id": name, "outcome": "succeeded"}
            ),
        ) as run:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(control.run_enabled(self.path, self.prod, self.config), 1)
        self.assertEqual(sorted(c.args[1] for c in run.call_args_list), [KY, NC])
        self.assertEqual({c.args[0] for c in run.call_args_list}, {IMAGE})
        latest = (self.root / "worker-latest.json").read_text()
        self.assertNotIn("writer-secret", latest)
        self.assertEqual(json.loads(latest)["outcome"], "failed")

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_source_held_by_a_backup_is_skipped_not_failed(self):
        with patch.object(
            control, "run_one", side_effect=lambda image, name, *a, **k: {"outcome": "locked"}
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(control.run_enabled(self.path, self.prod, self.config), 0)
        latest = json.loads((self.root / "worker-latest.json").read_text())
        self.assertEqual(latest["outcome"], "succeeded")

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_a_paused_source_stays_public_and_is_skipped_not_failed(self):
        self.assertEqual(control.pause(self.path, NC, True)["outcome"], "paused")
        self.assertTrue(catalog.read(self.path)["sources"][NC]["public"])
        with patch.object(control, "run_one", return_value={"outcome": "succeeded"}) as run:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(control.run_enabled(self.path, self.prod, self.config), 0)
        self.assertEqual([c.args[1] for c in run.call_args_list], [KY])
        latest = json.loads((self.root / "worker-latest.json").read_text())
        self.assertIn({"source_id": NC, "outcome": "paused"}, latest["results"])
        control.pause(self.path, NC, False)
        self.assertNotIn("paused", catalog.read(self.path)["sources"][NC])

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_certify_pauses_first_publishes_official_and_restores_the_pause_on_refusal(self):
        seen = []

        def run(image, name, status, prod, config, *, mode=None, extra=()):
            # The worker must not run the source while its official snapshot publishes.
            seen.append(
                (name, status, mode, catalog.read(self.path)["sources"][name].get("paused"))
            )
            self.assertEqual(extra, ())
            return outcome

        outcome = {"source_id": NC, "outcome": "failed", "error": "not marked official"}
        with (
            patch.object(control, "run_one", side_effect=run),
            self.assertRaisesRegex(ValueError, "not marked official"),
        ):
            control.certify(self.path, NC, self.prod, self.config)
        entry = catalog.read(self.path)["sources"][NC]
        self.assertEqual((entry["status"], entry.get("paused")), ("test", None))

        outcome = {"source_id": NC, "outcome": "succeeded", "raw_sha256": "a" * 64}
        with patch.object(control, "run_one", side_effect=run):
            self.assertEqual(
                control.certify(self.path, NC, self.prod, self.config)["outcome"], "certified"
            )
        entry = catalog.read(self.path)["sources"][NC]
        self.assertEqual((entry["status"], entry["paused"]), ("official", True))
        self.assertEqual(seen, [(NC, "official", None, True)] * 2)
        with patch.object(control, "run_one") as run:
            self.assertEqual(
                control.certify(self.path, NC, self.prod, self.config)["outcome"],
                "already_official",
            )
            run.assert_not_called()
        with self.assertRaisesRegex(ValueError, "public HTTP source"):
            control.certify(self.path, GA, self.prod, self.config)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_an_approved_snapshot_is_replayed_and_published_with_its_attestation(self):
        attested = {
            "evidence_url": "https://www.ncsbe.gov/news/certified",
            "certified_on": "2026-11-24",
            "attested_by": "Operator",
            "attested_at": "2026-11-24T20:00:00+00:00",
        }
        calls = []

        def run(image, name, status, prod, config, *, mode=None, extra=()):
            calls.append((status, extra))
            return {"source_id": NC, "outcome": "succeeded", "raw_sha256": "a" * 64}

        for approve, given, error in (
            ("a" * 64, None, "needs an attestation"),
            (None, attested, "needs an attestation"),
            ("A" * 64, attested, "full raw SHA-256"),
            ("a" * 64, {**attested, "evidence_url": "http://x"}, "https URL"),
        ):
            with (
                self.subTest(error=error),
                patch.object(control, "run_one", side_effect=run),
                self.assertRaisesRegex(ValueError, error),
            ):
                control.certify(
                    self.path, NC, self.prod, self.config, approve=approve, attested=given
                )
        # Refused before pausing anything.
        self.assertEqual((calls, catalog.read(self.path)["sources"][NC].get("paused")), ([], None))

        with patch.object(control, "run_one", side_effect=run):
            result = control.certify(
                self.path, NC, self.prod, self.config, approve="a" * 64, attested=attested
            )
        self.assertEqual((result["outcome"], result["attested"]), ("certified", True))
        status, extra = calls[0]
        self.assertEqual(status, "official")
        self.assertEqual(extra[:3], ("--replay-sha256", "a" * 64, "--publish-replay"))
        self.assertEqual(json.loads(extra[4]), {**attested, "mode": "approved_snapshot"})
        entry = catalog.read(self.path)["sources"][NC]
        self.assertEqual((entry["status"], entry["paused"]), ("official", True))

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_a_certified_file_from_the_inbox_is_imported_and_published_as_official(self):
        inbox = self.root / "data" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "nc-canvass.csv").write_text("certified", encoding="utf-8")
        attested = {
            "evidence_url": "https://www.ncsbe.gov/results-data/canvass.pdf",
            "certified_on": "2026-11-24",
            "attested_by": "Operator",
            "attested_at": "2026-11-24T20:00:00+00:00",
        }
        certified = {
            "file": inbox / "nc-canvass.csv",
            "sha256": "c" * 64,
            "downloaded_at": "2026-11-24T19:00:00+00:00",
            "name": "certified-results.csv",
        }
        jobs = []

        def job(image, prod, config, args, timeout):
            jobs.append(args)
            # The worker must not collect while the certified file publishes.
            self.assertTrue(catalog.read(self.path)["sources"][NC]["paused"])
            return json.dumps({"source_id": NC, "outcome": "succeeded", "raw_sha256": "d" * 64})

        with self.assertRaisesRegex(ValueError, "not both"):
            control.certify(
                self.path,
                NC,
                self.prod,
                self.config,
                approve="a" * 64,
                certified=certified,
                attested=attested,
            )
        outside = {**certified, "file": self.root / "elsewhere.csv"}
        outside["file"].write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            control.certify(
                self.path, NC, self.prod, self.config, certified=outside, attested=attested
            )
        self.assertNotIn("paused", catalog.read(self.path)["sources"][NC])

        with patch.object(control.containers, "run_job", side_effect=job):
            result = control.certify(
                self.path, NC, self.prod, self.config, certified=certified, attested=attested
            )
        self.assertEqual((result["outcome"], result["raw_sha256"]), ("certified", "d" * 64))
        args = jobs[0]
        self.assertEqual(args[0], "scripts/run_manual_source.py")
        self.assertEqual(args[args.index("--file") + 1], "/app/data/inbox/nc-canvass.csv")
        self.assertEqual(args[args.index("--certified") + 1], "certified-results.csv")
        self.assertEqual(args[args.index("--status") + 1], "official")
        self.assertIn("--publish", args)
        self.assertEqual(
            json.loads(args[args.index("--attestation") + 1]),
            {**attested, "mode": "certified_file"},
        )
        entry = catalog.read(self.path)["sources"][NC]
        self.assertEqual((entry["status"], entry["paused"]), ("official", True))

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_release_checks_an_official_source_as_unofficial(self):
        self.value["sources"][NC]["status"] = "official"
        self.save()
        statuses = {}

        def run(image, name, status, prod, config, *, mode=None, extra=()):
            statuses[name] = (status, mode)
            return {"source_id": name, "outcome": "validated"}

        with patch.object(control, "run_one", side_effect=run):
            failed = control.check_sources("image", catalog.read(self.path), self.prod, self.config)
        self.assertEqual(failed, {})
        self.assertEqual(statuses, {NC: ("unofficial", "check-only"), KY: ("test", "check-only")})

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_manual_and_disabled_sources_are_never_polled(self):
        self.value["sources"][KY].update(acquisition="manual")
        self.save()
        with patch.object(control, "run_one", return_value={"outcome": "succeeded"}) as run:
            with redirect_stdout(io.StringIO()):
                control.run_enabled(self.path, self.prod, self.config)
        self.assertEqual([c.args[1] for c in run.call_args_list], [NC])

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_parallel_jobs_run_at_the_same_time_and_need_a_bounded_count(self):
        # Neither job can finish until both have started, so a sequential run would stall.
        started = threading.Barrier(2, timeout=10)

        def run(image, name, status, prod, config):
            started.wait()
            return {"source_id": name, "outcome": "succeeded"}

        with patch.object(control, "run_one", side_effect=run):
            with redirect_stdout(io.StringIO()):
                config = {**self.config, "JOB_PARALLEL": "2"}
                self.assertEqual(control.run_enabled(self.path, self.prod, config), 0)
        for value in ("0", "5", "two"):
            with patch.object(control, "run_one") as run, self.assertRaises(ValueError):
                control.run_enabled(self.path, self.prod, {**self.config, "JOB_PARALLEL": value})
            run.assert_not_called()

    # Releases -------------------------------------------------------------------------

    def staged(self):
        return {"revision": NEW_REVISION, "image_id": NEW_IMAGE, "release_path": str(self.root)}

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock and symlinks")
    def test_release_moves_only_after_every_enabled_source_validates_under_the_new_image(self):
        validated = []

        def check(image, name, status, prod, config, *, mode=None):
            validated.append((image, name, mode))
            return {"source_id": name, "outcome": "validated"}

        with (
            patch.object(control, "describe", return_value=self.staged()),
            patch.object(control, "connect", MagicMock()),
            patch.object(control, "migrate", return_value=["024_shared_results.sql"]),
            patch.object(control, "run_one", side_effect=check),
            patch.object(control, "require_no_backup"),
        ):
            result = control.release(self.path, self.root, NEW_IMAGE, self.prod, self.config)
        self.assertEqual(result["outcome"], "released")
        self.assertEqual(
            sorted(validated), [(NEW_IMAGE, KY, "check-only"), (NEW_IMAGE, NC, "check-only")]
        )
        value = catalog.read(self.path)
        self.assertEqual(value["release"]["revision"], NEW_REVISION)
        self.assertEqual(value["previous_release"]["revision"], REVISION)
        self.assertEqual(Path(self.config["CURRENT_RELEASE"]).resolve(), self.root.resolve())

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_a_source_failing_validation_keeps_the_current_release(self):
        before = self.path.read_bytes()
        with (
            patch.object(control, "describe", return_value=self.staged()),
            patch.object(control, "connect", MagicMock()),
            patch.object(control, "migrate", return_value=[]),
            patch.object(
                control,
                "run_one",
                side_effect=lambda image, name, *a, **k: {
                    "source_id": name,
                    "outcome": "failed" if name == KY else "validated",
                    "error": "writer-secret parse failed",
                },
            ),
        ):
            with self.assertRaisesRegex(ValueError, "ky_2026_general") as caught:
                control.release(self.path, self.root, NEW_IMAGE, self.prod, self.config)
        self.assertNotIn("writer-secret", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(Path(self.config["CURRENT_RELEASE"]).exists())

    def test_only_a_named_moved_source_may_lack_a_snapshot_to_check(self):
        def check(error):
            def run(image, name, *a, **k):
                if name != KY:
                    return {"source_id": name, "outcome": "validated"}
                # As in production: the failed job exits nonzero, and the runner raises
                # with the job's report after its log lines.
                report = json.dumps({"source_id": name, "outcome": "failed", "error": error})
                raise RuntimeError('Command failed (docker, exit 1): {"log": "dbt"}\n' + report)

            return run

        with patch.object(control, "run_one", side_effect=check(NO_SNAPSHOT)):
            self.assertIn(
                NO_SNAPSHOT,
                control.check_sources(NEW_IMAGE, self.value, self.prod, self.config)[KY],
            )
            self.assertEqual(
                control.check_sources(NEW_IMAGE, self.value, self.prod, self.config, [KY]), {}
            )
            with self.assertRaisesRegex(ValueError, "Not an enabled HTTP source"):
                control.check_sources(NEW_IMAGE, self.value, self.prod, self.config, [GA])
        # Any other failure of a moved source still stops the release.
        with patch.object(control, "run_one", side_effect=check("Parsing failed")):
            self.assertIn(
                "Parsing failed",
                control.check_sources(NEW_IMAGE, self.value, self.prod, self.config, [KY])[KY],
            )
        self.assertFalse(control.no_snapshot('Command failed: {"error": "No archived'))
        self.assertFalse(control.no_snapshot(None))

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock and symlinks")
    def test_rollback_swaps_back_to_the_previous_release(self):
        self.value["previous_release"] = self.staged()
        self.save()
        with patch.object(control, "require_no_backup"):
            self.assertEqual(
                control.rollback_release(self.path, self.config)["revision"], NEW_REVISION
            )
            value = catalog.read(self.path)
            self.assertEqual(value["previous_release"]["revision"], REVISION)
            value["previous_release"] = None
            catalog.write(self.path, value)
            with self.assertRaisesRegex(ValueError, "No previous release"):
                control.rollback_release(self.path, self.config)

    # Dashboard activation and routing -------------------------------------------------

    def report(self, now, **changes):
        return {
            "outcome": "passed",
            "temporary_database_removed": True,
            "writer_permissions_verified": True,
            "reader_permissions_verified": True,
            "finished_at": (now - timedelta(minutes=1)).isoformat(),
            "database_inventory": {"serving.contest_results": {"rows": 3}},
            "current_publications": {GA: "publication-ga"},
            **changes,
        }

    def test_recovery_gate_requires_a_recent_restore_of_the_new_publication(self):
        now = datetime.now(timezone.utc)
        self.value["sources"][GA].update(
            publication_id="publication-ga", published_at=(now - timedelta(minutes=3)).isoformat()
        )
        control.recovery_gate(self.value, [GA], self.report(now), now)
        for changes in (
            {"current_publications": {}},
            {"database_inventory": {}},
            {"finished_at": (now - timedelta(hours=2)).isoformat()},
            {"finished_at": (now - timedelta(minutes=5)).isoformat()},
            {"reader_permissions_verified": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                control.recovery_gate(self.value, [GA], self.report(now, **changes), now)

    def caddyfile(self):
        path = self.root / "Caddyfile"
        path.write_text((ROOT / "deploy/Caddyfile").read_text(encoding="utf-8"), encoding="utf-8")
        return path, path.read_text(encoding="utf-8")

    def test_caddy_reload_failure_restores_file_and_never_stops_a_container(self):
        path, original = self.caddyfile()
        inode = path.stat().st_ino
        with patch.object(
            dashboard, "reload", side_effect=[RuntimeError("reload failed"), None]
        ) as reload:
            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                dashboard.handoff(
                    path,
                    "caddy-id",
                    "election-dashboard-" + "f" * 32 + ":8000",
                    self.root / "handoff.json",
                )
        self.assertEqual(path.read_text(), original)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(reload.call_args.args[1], original)
        self.assertEqual(
            json.loads((self.root / "handoff.json").read_text())["outcome"], "rolled_back"
        )

    def test_caddy_persistence_failure_restores_live_route_and_targets_are_owned(self):
        path, original = self.caddyfile()
        with (
            patch.object(dashboard, "reload") as reload,
            patch.object(dashboard, "persist", side_effect=[OSError("disk failed"), None]),
        ):
            with self.assertRaises(OSError):
                dashboard.handoff(
                    path,
                    "caddy-id",
                    "election-dashboard-" + "f" * 32 + ":8000",
                    self.root / "handoff.json",
                )
        self.assertEqual(reload.call_args.args[1], original)
        with self.assertRaisesRegex(ValueError, "upstream"):
            dashboard.handoff(path, "caddy-id", "dashboard:8000", self.root / "handoff.json")

    def test_active_and_previous_dashboard_cannot_be_discarded(self):
        name = "election-dashboard-" + "f" * 32
        self.value["previous_dashboard"] = {"name": name}
        with patch.object(dashboard.subprocess, "run") as run:
            for protected in (name, self.value["dashboard"]["name"]):
                with self.assertRaises(ValueError):
                    dashboard.stop_unused(protected, self.value)
        run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_failed_origin_check_rolls_back_route_and_catalog_together(self):
        path, original = self.caddyfile()
        after = copy.deepcopy(self.value)
        after["sources"][GA].update(enabled=True, public=True)
        before = self.path.read_bytes()
        with (
            patch.object(dashboard, "reload") as reload,
            patch.object(dashboard, "check", side_effect=ValueError("unhealthy new origin")),
        ):
            with self.assertRaisesRegex(ValueError, "unhealthy"):
                control.switch_dashboard(
                    self.path, self.value, after, "election-dashboard-" + "f" * 32, self.config
                )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(reload.call_args.args[1], original)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_pending_handoff_recovery_restores_catalog_and_route(self):
        path, original = self.caddyfile()
        journal = self.root / "handoff.json"
        after = copy.deepcopy(self.value)
        after["sources"][GA].update(enabled=True, public=True)
        with patch.object(dashboard, "reload"):
            dashboard.handoff(
                path,
                "caddy-id",
                "election-dashboard-" + "f" * 32 + ":8000",
                journal,
                before=self.value,
                catalog_file=self.path,
            )
            catalog.write(self.path, after)
            self.assertEqual(json.loads(journal.read_text())["outcome"], "pending")
            dashboard.recover(journal)
        self.assertEqual(catalog.public_states(catalog.read(self.path)), {"NC", "KY"})
        self.assertEqual(path.read_text(), original)

    def candidate(self, names=()):
        identity = "f" * 32
        return {
            "id": identity,
            "name": "election-dashboard-" + identity,
            "catalog_generation": self.value["generation"],
            "sources": list(names),
            "url": "http://new",
        }

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_same_coverage_switch_needs_no_restore_gate_and_keeps_a_rollback(self):
        self.caddyfile()
        with (
            patch.object(dashboard, "reload"),
            patch.object(dashboard, "check", return_value={"states": {}}),
            patch.object(control, "recovery_gate") as gate,
        ):
            result = control.activate(self.path, self.candidate(), {}, self.config)
            gate.assert_not_called()
            self.assertEqual(result["outcome"], "activated")
            value = catalog.read(self.path)
            self.assertEqual(value["dashboard"]["name"], self.candidate()["name"])
            self.assertEqual(value["previous_dashboard"]["name"], self.value["dashboard"]["name"])
            self.assertFalse(value["sources"][GA]["enabled"])
            control.rollback_dashboard(self.path, self.config)
        self.assertEqual(catalog.read(self.path)["dashboard"], self.value["dashboard"])

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_new_state_becomes_public_only_with_its_checked_publication(self):
        self.caddyfile()
        now = datetime.now(timezone.utc)
        self.value["sources"][GA].update(
            enabled=True,
            publication_id="publication-ga",
            published_at=(now - timedelta(minutes=3)).isoformat(),
        )
        self.save()
        served = {"states": {"GA": {"publication": {"publication_id": "publication-ga"}}}}
        with (
            patch.object(dashboard, "reload"),
            patch.object(dashboard, "check", return_value=served),
        ):
            with self.assertRaisesRegex(ValueError, "restore"):
                control.activate(self.path, self.candidate([GA]), {}, self.config)
            control.activate(self.path, self.candidate([GA]), self.report(now), self.config)
        self.assertEqual(catalog.public_states(catalog.read(self.path)), {"NC", "KY", "GA"})

    # Backups --------------------------------------------------------------------------

    def test_backup_records_the_catalog_and_both_releases(self):
        config = ROOT / "config/sources.toml"
        self.value["previous_release"] = self.staged()
        conn = MagicMock()
        conn.__enter__.return_value = conn

        def execute(statement, *args):
            if "pg_try_advisory_lock" in statement:
                return MagicMock(fetchone=lambda: {"ok": True})
            if "pg_export_snapshot" in statement:
                return MagicMock(fetchone=lambda: {"id": "snapshot"})
            return MagicMock(
                fetchall=lambda: [
                    {"schema_name": name} for name in ("ops", "landing", "serving", "ms", "nc")
                ]
                if "information_schema.schemata" in statement
                else []
            )

        conn.execute.side_effect = execute

        def dump(root, service, db, program, arguments, **kwargs):
            if program == "pg_dump":
                self.assertIn("--schema=landing", arguments)
                kwargs["stdout"].write(b"PGDMP")
            return ""

        def git(command, **kwargs):
            if "rev-parse" in command:
                return (REVISION if command[2] == str(self.root) else NEW_REVISION) + "\n"
            return ""

        with (
            patch.object(recovery, "connect", return_value=conn),
            patch.object(recovery, "inventory", return_value={}),
            patch.object(recovery, "docker_command", side_effect=dump),
            patch.object(recovery.subprocess, "check_output", side_effect=git),
        ):
            with self.assertRaisesRegex(ValueError, "Release checkout changed"):
                recovery.backup(
                    self.root,
                    self.root / "data",
                    config,
                    {"dbname": "elections", "user": "admin", "password": "secret"},
                    self.root / "backups",
                    "db",
                    catalog=self.value,
                )
            self.value["previous_release"] = None
            result = recovery.backup(
                self.root,
                self.root / "data",
                config,
                {"dbname": "elections", "user": "admin", "password": "secret"},
                self.root / "backups",
                "db",
                catalog=self.value,
            )
        manifest = recovery.verify_bundle(Path(result["backup_dir"]))
        self.assertIn("deployment-catalog.json", manifest["files"])
        self.assertEqual(list(manifest["collector_releases"]), [REVISION])

    # One-time cutover -----------------------------------------------------------------

    def test_cutover_keeps_flags_and_receipts_and_drops_retired_sources(self):
        old = {
            "version": 1,
            "generation": 41,
            "baseline": {"image_id": IMAGE, "revision": REVISION, "source_ids": []},
            "dashboard": {"name": "election-expansion-dashboard-x", "url": "u", "states": ["OH"]},
            "sources": {
                "nc_2026_general": {"worker": "baseline", "enabled": True, "public": False,
                                    "status": "test"},
                NC: {"worker": "expansion", "enabled": True, "public": True, "status": "test",
                     "publication_id": "p", "published_at": "2026-11-08T00:00:00+00:00",
                     "image_id": IMAGE, "races": []},
                "oh_2026_general": {"worker": "manual", "enabled": True, "public": True,
                                    "status": "unofficial", "manual_final": True,
                                    "manual_snapshot": {"publication_id": "o"}},
                "nc_absentee_early_demographics_2026": {"worker": "manual", "enabled": False,
                                                        "public": False, "status": "test"},
            },
        }  # fmt: skip
        value = cutover.convert_catalog(old, self.staged())
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["generation"], 42)
        self.assertNotIn("nc_2026_general", value["sources"])
        self.assertEqual(
            value["sources"][NC],
            {
                "enabled": True,
                "public": True,
                "status": "test",
                "publication_id": "p",
                "published_at": "2026-11-08T00:00:00+00:00",
                "acquisition": "http",
            },
        )
        self.assertEqual(value["sources"]["oh_2026_general"]["acquisition"], "manual")
        self.assertTrue(value["sources"]["oh_2026_general"]["manual_final"])
        demographics = value["sources"]["nc_absentee_early_demographics_2026"]
        self.assertEqual(demographics["acquisition"], "http")
        with self.assertRaises(ValueError):
            cutover.convert_catalog({**old, "version": 2}, self.staged())

    def test_cutover_settings_rename_budgets_and_drop_the_pinned_roots(self):
        old = {
            "BASELINE_ROOT": "/opt/election-pipeline",
            "CONTROL_ROOT": "/opt/election-releases/x",
            "PRODUCTION_ENV": "/etc/election-pipeline/production.env",
            "CATALOG_FILE": "/var/lib/election-pipeline-deployment/catalog.json",
            "CONTROL_STATE_DIR": "/var/lib/election-pipeline-deployment",
            "DOCKER_NETWORK": "election-production_default",
            "CADDYFILE": "/opt/election-pipeline/deploy/Caddyfile",
            "CADDY_CONTAINER": "abc",
            "ORIGIN_URL": "http://127.0.0.1",
            "EXPANSION_CPUS": "0.5",
            "EXPANSION_MEMORY": "768m",
            "EXPANSION_PARALLEL": "2",
            "JOB_TIMEOUT": "240",
            "CYCLE_SECONDS": "270",
        }
        text = cutover.convert_settings(old, Path("/opt/election-releases/current"))
        self.assertIn("JOB_PARALLEL=2\n", text)
        self.assertIn("CURRENT_RELEASE=", text)
        self.assertNotIn("CONTROL_ROOT", text)
        self.assertNotIn("BASELINE_ROOT", text)
        self.assertNotIn("EXPANSION_", text)
        # The pinned deployment defaulted a missing parallel setting to one job.
        old.pop("EXPANSION_PARALLEL")
        self.assertIn("JOB_PARALLEL=1\n", cutover.convert_settings(old, Path("/current")))

    def test_cutover_stops_before_copying_when_a_source_fails_under_the_new_image(self):
        old = {
            "version": 1,
            "generation": 1,
            "dashboard": {"name": "d", "url": "u", "states": ["NC"]},
            "sources": {
                NC: {"worker": "expansion", "enabled": True, "public": True, "status": "test"}
            },
        }
        self.path.write_text(json.dumps(old))
        settings = self.root / "deployment.env"
        settings.write_text(
            "".join(
                f"{key}={self.config.get(key, 'x')}\n" for key in (*cutover.KEPT, "PRODUCTION_ENV")
            ).replace("PRODUCTION_ENV=x", f"PRODUCTION_ENV={self.root / 'production.env'}")
        )
        (self.root / "production.env").write_text(
            "".join(f"{key}={value}\n" for key, value in self.prod.items())
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with (
            patch.object(cutover, "DEPLOYMENT_ENV", settings),
            patch.object(cutover, "recent_backup", return_value="backup_x"),
            patch.object(control, "describe", return_value=self.staged()),
            patch.object(cutover, "connect", return_value=connection),
            patch.object(cutover, "migrate", return_value=["024_shared_results.sql"]),
            patch.object(control, "check_sources", return_value={NC: "validation_failed"}),
            patch.object(cutover, "stop_collectors") as stop,
            patch.object(cutover, "start_collectors") as start,
            patch.object(cutover, "consolidate") as copy,
            patch.object(cutover, "replace_private") as rewrite,
            patch("sys.argv", ["cutover", "--release", str(self.root), "--image", NEW_IMAGE,
                               "--apply"]),
        ):  # fmt: skip
            with self.assertRaisesRegex(ValueError, "did not validate"):
                cutover.main()
        stop.assert_called_once()
        start.assert_called_once()
        copy.assert_not_called()
        rewrite.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), old)


if __name__ == "__main__":
    unittest.main()
