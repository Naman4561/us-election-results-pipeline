"""One release for every source: catalog rules, the worker, release switches and routing."""

import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from election_pipeline import publication, recovery
from election_pipeline.dashboard.server import create_app
from election_pipeline.deployment import catalog, containers, control, dashboard
from election_pipeline.settings import load_config
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

    def test_the_source_configuration_is_parsed_once_per_version(self):
        providers = {"NC": lambda: {"publication": None}, "KY": lambda: {"publication": None}}
        client = TestClient(create_app(state_providers=providers, catalog_file=self.path))
        catalog.parsed.cache_clear()
        with patch.object(catalog, "load_config", wraps=catalog.load_config) as load:
            for _ in range(3):
                self.assertEqual(client.get("/api/national").status_code, 200)
            self.assertEqual(load.call_count, 1)
            catalog.configured().clear()  # callers get their own copy
            self.assertIn(NC, catalog.configured())

    def test_a_dashboard_may_have_its_own_cpu_budget(self):
        def cpus(arguments):
            return arguments[arguments.index("--cpus") + 1]

        name = "election-dashboard-" + "0" * 32
        self.assertEqual(cpus(containers.base_args(name, IMAGE, self.config)), "0.5")
        self.assertEqual(cpus(containers.base_args(name, IMAGE, self.config, cpus="1.0")), "1.0")
        with self.assertRaisesRegex(ValueError, "CPU budget"):
            containers.base_args(name, IMAGE, self.config, cpus="all")

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
    def test_register_adds_a_new_state_disabled_private_and_once(self):
        name = "ut_2026_general"
        self.assertNotIn(name, catalog.read(self.path)["sources"])
        self.assertEqual(control.register(self.path, name)["outcome"], "registered")
        self.assertEqual(
            catalog.read(self.path)["sources"][name],
            {"acquisition": "http", "enabled": False, "public": False, "status": "unofficial"},
        )
        with self.assertRaisesRegex(ValueError, "already in the catalog"):
            control.register(self.path, name)
        with self.assertRaisesRegex(ValueError, "configured in this release"):
            control.register(self.path, "zz_2026_general")
        with self.assertRaisesRegex(ValueError, "configured in this release"):
            control.register(self.path, "nc_absentee_early_demographics_2026")

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

    def idle_source(self, name, checked_minutes_ago=1):
        """A collected source holding snapshot "a…" and a recent run report."""
        settings, sources = load_config(catalog.CONFIG)
        data = Path(self.prod["DATA_DIR"])
        (data / "state").mkdir(parents=True, exist_ok=True)
        (data / "runs" / name).mkdir(parents=True, exist_ok=True)
        checked = datetime.now(timezone.utc) - timedelta(minutes=checked_minutes_ago)
        (data / "state" / f"{name}.json").write_text(
            json.dumps(
                {
                    "sha256": "a" * 64,
                    "url": sources[name].url,
                    "source_signature": sources[name].signature(settings),
                    "last_checked_at": checked.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        (data / "runs" / name / "latest.json").write_text("{}", encoding="utf-8")
        return settings, sources

    def test_a_source_is_idle_only_while_a_run_would_change_nothing(self):
        settings, sources = self.idle_source(NC)
        entry = self.value["sources"][NC]
        ledger = {NC: {"image": IMAGE, "status": "test", "raw_sha256": "a" * 64}}
        now = datetime.now(timezone.utc)

        def idle(**changes):
            arguments = {"entry": entry, "image": IMAGE, "ledger": ledger, "now": now, **changes}
            return control.idle(
                NC,
                arguments["entry"],
                arguments["image"],
                self.prod,
                arguments["ledger"],
                settings,
                sources,
                arguments["now"],
            )

        self.assertTrue(idle())
        self.assertFalse(idle(image=NEW_IMAGE))  # a new release
        self.assertFalse(idle(entry={**entry, "status": "unofficial"}))
        self.assertFalse(idle(ledger={}))  # its last run did not publish
        self.assertFalse(idle(now=now + timedelta(minutes=14)))  # due for collection
        report = Path(self.prod["DATA_DIR"]) / "runs" / NC / "latest.json"
        old = time.time() - control.IDLE_RUN_SECONDS - 60
        os.utime(report, (old, old))
        self.assertFalse(idle())  # its run report is due a refresh
        self.idle_source(NC)
        state = Path(self.prod["DATA_DIR"]) / "state" / f"{NC}.json"
        state.write_text(state.read_text().replace("a" * 64, "b" * 64), encoding="utf-8")
        self.assertFalse(idle())  # a new snapshot
        self.idle_source(NC)
        value = json.loads(state.read_text())
        state.write_text(json.dumps({**value, "consecutive_failures": 1}), encoding="utf-8")
        self.assertFalse(idle())

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_the_worker_skips_idle_sources_and_records_what_it_ran(self):
        for name in (NC, KY):
            self.idle_source(name)
        ran = []

        def run(image, name, status, prod, config):
            ran.append(name)
            outcome = "failed" if name == KY else "already_current"
            return {"source_id": name, "outcome": outcome, "raw_sha256": "a" * 64}

        def cycle():
            ran.clear()
            with patch.object(control, "run_one", side_effect=run):
                with redirect_stdout(io.StringIO()):
                    control.run_enabled(self.path, self.prod, self.config)
            return sorted(ran)

        self.assertEqual(cycle(), [KY, NC])
        ledger = json.loads((self.root / control.IDLE_LEDGER).read_text())["sources"]
        self.assertEqual(set(ledger), {NC})  # a failed run is never skipped
        self.assertEqual(cycle(), [KY])
        latest = json.loads((self.root / "worker-latest.json").read_text())["results"]
        self.assertIn({"source_id": NC, "outcome": "not_due", "skipped": "idle"}, latest)

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
            patch.object(control, "prune_releases", side_effect=RuntimeError("docker down")),
        ):
            result = control.release(self.path, self.root, NEW_IMAGE, self.prod, self.config)
        self.assertEqual(result["outcome"], "released")
        self.assertEqual(result["prune_error"], "docker down")  # reported, release stands
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

    def release_tree(self):
        """A release checkout of what a source check runs, with NC and KY snapshots."""
        tree = self.root / "release"
        ignore = shutil.ignore_patterns("__pycache__", "target", "logs")
        for entry in (*control.CHECK_INPUTS, "config/sources.toml"):
            source, target = ROOT / entry, tree / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=ignore)
            elif source.exists():
                shutil.copy2(source, target)
            else:
                # Run from inside the image, which has no Dockerfile of its own.
                target.write_text(f"stand-in for {entry}\n", encoding="utf-8")
        settings, sources = load_config(tree / "config/sources.toml")
        state = Path(self.prod["DATA_DIR"]) / "state"
        state.mkdir(parents=True)
        for name in (NC, KY):
            (state / f"{name}.json").write_text(
                json.dumps({"sha256": "a" * 64, "url": sources[name].url}), encoding="utf-8"
            )
        return tree

    def test_a_check_key_changes_only_with_what_that_source_check_runs(self):
        tree = self.release_tree()

        def keys():
            return control.check_keys(tree, self.value, self.prod, [NC, KY])

        def changed(relative, edit=lambda text: text + "\n# changed\n"):
            path = tree / relative
            before = keys()
            path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
            after = keys()
            return {name for name in before if before[name] != after[name]}

        package = "src/election_pipeline/"
        self.assertEqual(changed(package + "parsers/ky.py"), {KY})
        self.assertEqual(changed(package + "parsers/nc.py"), {NC})
        # Another state's parser and data, the dashboard and the host-side tools: neither.
        self.assertEqual(changed(package + "parsers/ms.py"), set())
        self.assertEqual(changed(package + "resources/ms_2026_counties.json", str.rstrip), set())
        # Every parser imports every roster.
        self.assertEqual(changed(package + "resources/rosters_2026/OK.json", str.rstrip), {NC, KY})
        self.assertEqual(changed(package + "dashboard/server.py"), set())
        self.assertEqual(changed(package + "deployment/control.py"), set())
        # Shared parsing, the database models and the image: both.
        self.assertEqual(changed(package + "parsers/results.py"), {NC, KY})
        # The registry counts as far as it concerns each state; its shared code for all.
        self.assertEqual(
            changed(package + "states.py", lambda text: text.replace("liveresults/", "live/")),
            {KY},
        )
        self.assertEqual(
            changed(
                package + "states.py",
                lambda text: text.replace(
                    '    "KY": State(',
                    '    "ZZ": State("ZZ", "Z", "zz", (0,), ("total_votes",)),\n    "KY": State(',
                    1,
                ),
            ),
            set(),
        )
        self.assertEqual(changed(package + "states.py"), set())  # a comment
        self.assertEqual(
            changed(package + "states.py", lambda text: text + "\n\ndef helper():\n    return 1\n"),
            {NC, KY},
        )
        # So does what makes a publication: a state added elsewhere republishes no other.
        registry = tree / package / "states.py"

        def hashes():
            # Callers pass a resolved root, as the module's own path is (8.3 names differ).
            root = tree.resolve()
            return publication.project_hash(root, "NC"), publication.project_hash(root, "KY")

        with patch.object(
            publication, "__file__", str(tree.resolve() / package / "publication.py")
        ):
            before = hashes()
            registry.write_text(
                registry.read_text(encoding="utf-8").replace("ky.gov/live/", "ky.gov/results/"),
                "utf-8",
            )
            after = hashes()
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(changed("dbt/dbt_project.yml"), {NC, KY})
        self.assertEqual(changed("constraints.txt"), {NC, KY})
        # A new snapshot or status changes only that source's key.
        before = keys()
        state = Path(self.prod["DATA_DIR"]) / "state" / f"{NC}.json"
        state.write_text(state.read_text().replace("a" * 64, "b" * 64), encoding="utf-8")
        self.value["sources"][KY]["status"] = "unofficial"
        after = keys()
        self.assertTrue(before[NC] != after[NC] and before[KY] != after[KY])
        # A source without a snapshot of its configured address gets no key.
        state.write_text(json.dumps({"sha256": "b" * 64, "url": "https://old"}), "utf-8")
        self.assertEqual(set(keys()), {KY})

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock and symlinks")
    def test_a_release_reuses_passed_checks_with_unchanged_keys(self):
        checked, keys = [], {NC: "n" * 64, KY: "k" * 64}

        def check(image, name, *a, **k):
            checked.append(name)
            return {"source_id": name, "outcome": "failed" if name in failing else "validated"}

        def release(**options):
            checked.clear()
            catalog.write(self.path, self.value)
            with (
                patch.object(control, "describe", return_value=self.staged()),
                patch.object(control, "connect", MagicMock()),
                patch.object(control, "migrate", return_value=[]),
                patch.object(control, "check_keys", return_value=dict(keys)),
                patch.object(control, "run_one", side_effect=check),
                patch.object(control, "require_no_backup"),
                patch.object(control, "prune_releases", return_value={}),
            ):
                return control.release(
                    self.path, self.root, NEW_IMAGE, self.prod, self.config, **options
                )

        failing = {KY}
        with self.assertRaisesRegex(ValueError, "ky_2026_general"):
            release()
        self.assertEqual(sorted(checked), [KY, NC])
        # NC's pass stands although that release failed; KY is checked again.
        failing = set()
        self.assertEqual(release()["sources_reused"], [NC])
        self.assertEqual(checked, [KY])
        # With nothing changed, one source is still checked.
        self.assertEqual(len(release()["sources_reused"]), 1)
        self.assertEqual(len(checked), 1)
        keys[KY] = "x" * 64
        self.assertEqual(release()["sources_validated"], [KY])
        self.assertEqual(sorted(release(check_all=True)["sources_validated"]), [KY, NC])
        # A source without a key (no snapshot, or none computed) is always checked.
        keys.clear()
        self.assertEqual(sorted(release()["sources_validated"]), [KY, NC])

    def test_a_locked_source_is_waited_for_rather_than_failing_the_release(self):
        outcomes = {NC: ["locked", "locked", "validated"], KY: ["validated"]}

        def run(image, name, *a, **k):
            return {"source_id": name, "outcome": outcomes[name].pop(0)}

        with (
            patch.object(control, "run_one", side_effect=run) as calls,
            patch.object(control, "LOCK_RETRY_SECONDS", 0),
        ):
            failed = control.check_sources(NEW_IMAGE, self.value, self.prod, self.config)
        self.assertEqual(failed, {})
        self.assertEqual(sum(c.args[1] == NC for c in calls.call_args_list), 3)
        # Past the wait, a source still locked fails the release as before.
        with (
            patch.object(control, "run_one", return_value={"source_id": NC, "outcome": "locked"}),
            patch.object(control, "LOCK_RETRY_SECONDS", 0),
        ):
            config = {**self.config, "CHECK_LOCK_WAIT_SECONDS": "0"}
            failed = control.check_sources(NEW_IMAGE, self.value, self.prod, config)
        self.assertEqual(failed[NC], "locked")

    @unittest.skipIf(os.name == "nt", "Linux release symlink")
    def test_pruning_keeps_the_releases_dashboards_and_environments_still_needed(self):
        releases, environments = self.root / "releases", self.root / "venvs"
        revisions = {letter: letter * 40 for letter in "abcdefg"}
        for age, letter in enumerate("gfedcba"):  # a is the oldest, g the newest
            folder = releases / revisions[letter]
            (folder / ".venv/lib/python3.12/site-packages").mkdir(parents=True)
            os.utime(folder, (2000 - age, 2000 - age))
        (releases / "current").symlink_to(releases / revisions["g"])
        for key in ("1" * 16, "2" * 16, "3" * 16):
            (environments / key).mkdir(parents=True)
        os.utime(environments / ("1" * 16), (0, 0))
        os.utime(environments / ("2" * 16), (0, 0))
        packages = releases / revisions["g"] / ".venv/lib/python3.12/site-packages"
        (packages / "election-dependencies.pth").write_text(
            f'import site; site.addsitedir("{environments}/{"2" * 16}/lib/python3.12/x")\n'
        )
        value = {
            "release": {"revision": revisions["g"]},
            "previous_release": {"revision": revisions["f"]},
        }
        commands = []

        def command(args, **kwargs):
            commands.append(args)
            if args[:2] == ["docker", "ps"]:
                return "container-1"
            if args[:2] == ["docker", "inspect"]:
                return "sha256:image-a"  # the previous dashboard runs the oldest release
            if args[:2] == ["docker", "images"]:
                return "\n".join(f"{r} sha256:image-{k}" for k, r in revisions.items())
            return ""

        config = {
            "CURRENT_RELEASE": str(releases / "current"),
            "HOST_ENVIRONMENTS": str(environments),
        }
        with patch.object(control.containers, "command", side_effect=command):
            removed = control.prune_releases(config, value)
        # Current g, previous f, a (in use) and the two newest others e and d are kept.
        self.assertEqual(sorted(removed["releases"]), [revisions["b"], revisions["c"]])
        self.assertEqual(
            sorted(p.name for p in releases.iterdir()),
            sorted(["current", *(revisions[k] for k in "adefg")]),
        )
        self.assertEqual(sorted(removed["images"]), [revisions["b"], revisions["c"]])
        # Environment 2 is named by a kept release; 3 changed within the hour.
        self.assertEqual(removed["environments"], ["1" * 16])
        self.assertIn(
            ["docker", "builder", "prune", "-f", "--max-used-space", str(4 * 1024**3)], commands
        )
        # A current release that would not be kept stops pruning before anything goes.
        (releases / "current").unlink()
        (releases / "current").symlink_to(releases / revisions["d"])
        with (
            patch.object(control.containers, "command", side_effect=command),
            self.assertRaisesRegex(ValueError, "current release"),
        ):
            control.prune_releases(config, {"release": {"revision": revisions["g"]}})
        self.assertTrue((releases / revisions["d"]).is_dir())
        # Folders that hold or sit inside the data or backups are never pruned.
        for guarded in (releases, releases / revisions["a"], self.root):
            with (
                patch.object(control.containers, "command", side_effect=command) as calls,
                self.assertRaisesRegex(ValueError, "Refusing to prune"),
            ):
                control.prune_releases(config, value, keep_out=[guarded.resolve()])
            calls.assert_not_called()
        self.assertEqual(
            control.protected({"DATA_DIR": "/srv/d"}, {"BACKUP_DIR": "/srv/b"}),
            [Path("/srv/d").resolve(), Path("/srv/b").resolve()],
        )

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

    def test_prune_removes_every_candidate_but_the_active_and_previous(self):
        previous, stale, other = ("election-dashboard-" + c * 32 for c in "fea")
        self.value["previous_dashboard"] = {"name": previous}
        listed = " ".join([self.value["dashboard"]["name"], previous, stale, other, "caddy"])
        with (
            patch.object(dashboard.containers, "command", return_value=listed),
            patch.object(dashboard.subprocess, "run") as run,
        ):
            self.assertEqual(dashboard.prune(self.value), [other, stale])
        self.assertEqual([c.args[0][-1] for c in run.call_args_list], [other, stale])

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
            patch.object(dashboard, "prune", return_value=["stale"]) as prune,
        ):
            result = control.activate(self.path, self.candidate(), {}, self.config)
            gate.assert_not_called()
            self.assertEqual(result["outcome"], "activated")
            self.assertEqual(result["discarded"], ["stale"])
            value = catalog.read(self.path)
            kept = prune.call_args.args[0]
            self.assertEqual(kept["dashboard"]["name"], self.candidate()["name"])
            self.assertEqual(kept["previous_dashboard"]["name"], self.value["dashboard"]["name"])
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
            patch.object(dashboard, "prune", side_effect=RuntimeError("docker is down")),
        ):
            with self.assertRaisesRegex(ValueError, "restore"):
                control.activate(self.path, self.candidate([GA]), {}, self.config)
            result = control.activate(
                self.path, self.candidate([GA]), self.report(now), self.config
            )
        self.assertIn("docker is down", result["discard_error"])
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
