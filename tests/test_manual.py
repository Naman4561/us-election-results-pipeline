"""Offline acquisition, rejection, publication ownership and manual freshness."""

import copy
import gzip
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient

from election_pipeline import manual
from election_pipeline.dashboard.server import create_app
from election_pipeline.deployment import catalog, containers, control
from election_pipeline.deployment import manual as deployment_manual
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "manual_monitor", ROOT / "scripts/monitor_production.py"
)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class ManualTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["oh_2026_general"]
        self.raw = gzip.decompress(
            (ROOT / "tests/fixtures/five_states/oh-results.xml.gz").read_bytes()
        )
        self.file = self.root / "download.xml"
        self.file.write_bytes(self.raw)
        self.data = self.root / "data"
        self.observed = "2026-11-04T05:00:00+00:00"

    def acquire(self, raw=None, **kwargs):
        if raw is not None:
            self.file.write_bytes(raw)
        return manual.import_file(
            self.source,
            self.settings,
            self.data,
            self.file,
            kwargs.pop("downloaded_at", self.observed),
            kwargs.pop("expected_sha256", hashlib.sha256(self.file.read_bytes()).hexdigest()),
            **kwargs,
        )

    def test_official_file_import_is_offline_replayable_deterministic_and_not_an_http_poll(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No HTTP during import")):
            receipt = self.acquire()
            repeated = self.acquire()
        self.assertEqual(receipt["sha256"], repeated["sha256"])
        self.assertFalse(repeated["archive_created"])
        self.assertEqual(receipt["outcome"], "manual_imported")
        self.assertIsNone(receipt["http_status"])
        self.assertEqual(receipt["acquisition_method"], "manual")
        self.assertEqual(receipt["downloaded_at"], self.observed)
        self.assertEqual(receipt["original_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(ArchiveStore(self.data, self.source).read_state(), {})
        self.assertTrue(Path(receipt["metadata_path"]).is_file())

    def test_bad_file_hash_html_wrong_election_dropped_county_and_corrupt_votes_never_commit(self):
        original = ET.fromstring(self.raw)
        variants = [b"<html>Cloudflare challenge</html>"]
        wrong = copy.deepcopy(original)
        next(e for e in wrong.iter() if e.tag.endswith("}Election") or e.tag == "Election").set(
            "Date", "2024-11-05"
        )
        variants.append(ET.tostring(wrong))
        wrong = copy.deepcopy(original)
        parent = next(
            e
            for e in wrong.iter()
            if e.tag.endswith("}GpUnitCollection") or e.tag == "GpUnitCollection"
        )
        county = next(
            e
            for e in parent
            if e.get("Type") == "County" and e.get("IsElectoralDistrict") == "false"
        )
        parent.remove(county)
        variants.append(ET.tostring(wrong))
        wrong = copy.deepcopy(original)
        vote = next(
            e for e in wrong.iter() if e.tag.endswith("}VoteCounts") or e.tag == "VoteCounts"
        )
        vote.set("Count", str(int(vote.get("Count")) + 1))
        variants.append(ET.tostring(wrong))
        for raw in variants:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                self.acquire(raw)
        self.file.write_bytes(self.raw)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.acquire(expected_sha256="0" * 64)
        self.assertFalse((self.data / "raw").exists())
        self.assertFalse((self.data / "metadata").exists())

    def test_naive_future_download_and_old_report_are_rejected(self):
        with self.assertRaises(ValueError):
            self.acquire(downloaded_at="2026-11-04T05:00:00")
        with self.assertRaises(ValueError):
            self.acquire(downloaded_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(ValueError, "older"):
            self.acquire(minimum="2026-11-04T05:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "generation time"):
            self.acquire(downloaded_at="2026-11-03T23:00:00+00:00")

    def test_import_runs_real_parser_then_normal_load_and_candidate_validation_without_collection(
        self,
    ):
        from election_pipeline import database, publication, workflow

        receipt = self.acquire()
        candidate = str(uuid.uuid4())
        with (
            patch.object(workflow, "collect_source", side_effect=AssertionError("No HTTP")),
            patch.object(database, "connection_settings", return_value={"password": "secret"}),
            patch.object(
                database, "load_parsed", return_value={"outcome": "succeeded", "release_id": 7}
            ) as load,
            patch.object(
                publication,
                "prepare",
                return_value={"outcome": "validated", "candidate_id": candidate},
            ) as prepare,
            patch.object(
                publication, "publish", side_effect=AssertionError("Initial import remains private")
            ),
        ):
            result = workflow.run_source(
                self.source,
                self.settings,
                self.data,
                root=ROOT,
                status="unofficial",
                replay_sha256=receipt["sha256"],
                prepare_only=True,
            )
        self.assertEqual(result["outcome"], "validated")
        self.assertEqual(result["candidate_id"], candidate)
        self.assertEqual(
            [s["stage"] for s in result["stages"]], ["parse", "load", "validate_candidate"]
        )
        load.assert_called_once()
        prepare.assert_called_once()


class ManualDeploymentTests(unittest.TestCase):
    def setUp(self):
        from test_deployment import fixture

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        self.value = fixture(self.root)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["oh_2026_general"]
        self.image = "sha256:" + "a" * 64
        self.revision = "b" * 40
        entry = catalog.entry(
            self.source,
            self.settings,
            image_id=self.image,
            revision=self.revision,
            release_path=self.root,
            worker="manual",
            status="unofficial",
        )
        entry.update(races=list(state_adapter("OH").races))
        self.value["sources"][self.source.source_id] = entry
        catalog.write(self.path, self.value)
        self.prod = {
            "DATA_DIR": str(self.root / "data"),
            "IMAGE_TAG": self.revision,
            "PIPELINE_SOURCES": "nc_2026_general,ky_2026_general",
        }
        self.config = {
            "CONTROL_STATE_DIR": str(self.root),
            "EXPANSION_CPUS": "0.5",
            "EXPANSION_MEMORY": "768m",
            "CADDYFILE": str(self.root / "Caddyfile"),
            "CADDY_CONTAINER": "caddy",
            "DEPLOYMENT_CATALOG": str(self.path),
            "BACKUP_DIR": str(self.root / "backups"),
        }

    def save(self):
        catalog.write(self.path, self.value)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_configure_keeps_source_private_and_preserves_every_other_source(self):
        before = catalog.read(self.path)
        meta = {
            "source": json.loads(json.dumps(asdict(self.source))),
            "settings": asdict(self.settings),
            "races": self.value["sources"][self.source.source_id]["races"],
            "code_sha256": "c" * 64,
        }
        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "db_settings", return_value={}),
            patch.object(control, "connect"),
            patch.object(catalog, "verify_baseline"),
            patch.object(containers, "verify_release", return_value=(self.image, self.revision)),
            patch.object(containers, "metadata", return_value=meta),
            patch.object(containers, "package_hash", return_value="c" * 64),
            patch.object(containers, "release_hash", return_value="d" * 64),
        ):
            deployment_manual.configure(
                self.path, self.root, self.image, self.source.source_id, self.prod, self.config
            )
            after = catalog.read(self.path)
            for name in self.value["sources"]:
                if name != self.source.source_id:
                    self.assertEqual(after["sources"][name], before["sources"][name])
            self.assertFalse(after["sources"][self.source.source_id]["public"])
            after["sources"][self.source.source_id].update(public=True, enabled=True)
            catalog.write(self.path, after)
            with self.assertRaisesRegex(ValueError, "disabled"):
                deployment_manual.configure(
                    self.path, self.root, self.image, self.source.source_id, self.prod, self.config
                )

    def test_scheduled_worker_ignores_public_manual_source_but_runs_automatic_expansion(self):
        self.value["sources"][self.source.source_id].update(public=True, enabled=True)
        self.value["sources"]["ga_2026_general"].update(public=True, enabled=True)
        self.save()
        with (
            patch.object(catalog, "verify_baseline"),
            patch.object(control, "run_one", return_value={"outcome": "succeeded"}) as run,
        ):
            self.assertEqual(control.run_enabled(self.path, self.prod, self.config), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0]["source"]["state"], "GA")

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_initial_direct_publication_and_outside_inbox_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Initial"):
            deployment_manual.operate(
                self.path, self.source.source_id, self.prod, self.config, publish=True
            )
        file = self.root / "outside.xml"
        file.write_text("secret")
        with self.assertRaises(ValueError):
            deployment_manual.operate(
                self.path, self.source.source_id, self.prod, self.config, file=file
            )

    def test_manual_worker_error_in_stdout_is_visible(self):
        result = subprocess.CompletedProcess(
            ["docker"], 1, stdout='{"error":"hash mismatch"}', stderr=""
        )
        with (
            patch.object(subprocess, "run", return_value=result),
            self.assertRaisesRegex(RuntimeError, "hash mismatch"),
        ):
            containers.command(["docker", "run"])

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_publishing_new_manual_snapshot_clears_an_older_prepared_candidate(self):
        entry = self.value["sources"][self.source.source_id]
        entry.update(
            public=True,
            enabled=True,
            candidate_id=str(uuid.uuid4()),
            manual_candidate={"old": True},
        )
        self.save()
        local = self.root / "data/inbox/ohio.xml"
        local.parent.mkdir(parents=True)
        local.write_text("file")
        receipt = {
            k: "value"
            for k in (
                "downloaded_at",
                "imported_at",
                "source_generated_at",
                "original_sha256",
                "sha256",
                "metadata_path",
            )
        }
        result = {
            "source_id": self.source.source_id,
            "outcome": "succeeded",
            "publication_id": str(uuid.uuid4()),
            "manual_acquisition": receipt,
        }
        with patch.object(containers, "run_job", return_value=json.dumps(result)):
            deployment_manual.operate(
                self.path,
                self.source.source_id,
                self.prod,
                self.config,
                file=local,
                sha256="a" * 64,
                downloaded_at="now",
                publish=True,
            )
        current = catalog.read(self.path)["sources"][self.source.source_id]
        self.assertNotIn("candidate_id", current)
        self.assertNotIn("manual_candidate", current)
        self.assertEqual(current["manual_snapshot"]["publication_id"], result["publication_id"])

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_manual_activation_preserves_restore_gate_and_enables_only_selected_state(self):
        now = datetime.now(timezone.utc)
        entry = self.value["sources"][self.source.source_id]
        entry.update(
            publication_id="manual-p", published_at=(now - timedelta(minutes=3)).isoformat()
        )
        self.save()
        (self.root / "Caddyfile").write_text("old")
        candidate = {
            "id": "a" * 32,
            "name": "election-expansion-dashboard-" + "a" * 32,
            "url": "http://candidate",
            "catalog_generation": 0,
            "sources": [self.source.source_id],
            "states": ["NC", "KY", "OH"],
        }
        report = {
            "outcome": "passed",
            "temporary_database_removed": True,
            "writer_permissions_verified": True,
            "reader_permissions_verified": True,
            "finished_at": now.isoformat(),
            "current_publications": {self.source.source_id: "manual-p"},
            "database_inventory": {"oh.contest_candidate_results": {"rows": 7}},
        }
        payload = {"states": {"OH": {"publication": {"publication_id": "manual-p"}}}}
        with (
            patch.object(control, "controls_ready"),
            patch.object(catalog, "verify_baseline"),
            patch.object(control.dashboard, "check", return_value=payload),
            patch.object(control, "switch_dashboard") as switch,
        ):
            result = control.activate(self.path, candidate, report, self.prod, self.config)
            after = switch.call_args.args[2]
            self.assertEqual(result["outcome"], "activated")
            self.assertEqual(after["sources"][self.source.source_id]["worker"], "manual")
            self.assertTrue(after["sources"][self.source.source_id]["public"])
            self.assertFalse(after["sources"]["ga_2026_general"]["public"])
            report["current_publications"][self.source.source_id] = "wrong"
            with self.assertRaisesRegex(ValueError, "snapshot"):
                control.activate(self.path, candidate, report, self.prod, self.config)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_dashboard_api_marks_manual_source_and_rollback_removes_only_new_coverage(self):
        entry = self.value["sources"][self.source.source_id]
        entry.update(public=True, enabled=True)
        self.save()
        providers = {
            code: (lambda c=code: {"state": c, "publication": {"publication_id": "p"}, "races": {}})
            for code in ("NC", "KY", "OH")
        }
        app = create_app(state_providers=providers, catalog_file=self.path)
        with TestClient(app) as client:
            payload = client.get("/api/national").json()
        self.assertEqual(payload["states"]["OH"]["acquisition"]["method"], "manual")
        self.assertNotIn("acquisition", payload["states"]["NC"])
        self.value["previous_dashboard"] = {
            "name": "previous",
            "url": "http://previous",
            "states": ["NC", "KY"],
            "caddyfile": "old",
        }
        self.save()
        with (
            patch.object(control.dashboard, "check"),
            patch.object(control, "switch_dashboard") as switch,
        ):
            control.rollback(self.path, self.config)
        after = switch.call_args.args[2]
        self.assertFalse(after["sources"][self.source.source_id]["public"])
        self.assertTrue(after["sources"]["nc_2026_general"]["public"])

    def test_manual_freshness_does_not_require_http_poll_but_overdue_import_alerts(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=2)).isoformat()
        entry = self.value["sources"][self.source.source_id]
        entry.update(
            public=True,
            enabled=True,
            publication_id="manual-p",
            manual_snapshot={"downloaded_at": recent, "publication_id": "manual-p"},
        )
        self.save()
        for name in self.prod["PIPELINE_SOURCES"].split(","):
            atomic_json(
                self.root / "data/runs" / name / "latest.json",
                {"outcome": "succeeded", "finished_at": recent},
            )
            atomic_json(self.root / "data/state" / (name + ".json"), {"last_checked_at": recent})
        atomic_json(
            self.root / "backups/latest-backup.json", {"outcome": "uploaded", "finished_at": recent}
        )
        payload = {
            "mode": "published",
            "states": {
                code: {
                    "availability": "available",
                    "races": {"a": {}},
                    "publication": {"publication_id": "manual-p" if code == "OH" else "p"},
                }
                for code in ("NC", "KY", "OH")
            },
        }
        with (
            patch.object(monitor, "load_config", return_value=(None, {})),
            patch.object(catalog, "verify_baseline"),
            patch.object(containers, "inspect_image", return_value=(self.image, self.revision)),
            patch.object(
                monitor, "systemd", side_effect=lambda action, unit: action == "is-active"
            ),
            patch.object(monitor, "dashboard_payload", return_value=payload),
            patch.object(
                monitor.shutil, "disk_usage", return_value=SimpleNamespace(total=100, free=90)
            ),
        ):
            self.assertEqual(monitor.assess(self.prod, self.config, now), {})
            entry["manual_snapshot"]["downloaded_at"] = (now - timedelta(hours=7)).isoformat()
            self.save()
            issues = monitor.assess(self.prod, self.config, now)
        self.assertIn("manual/oh_2026_general", issues)
        self.assertNotIn("poll/oh_2026_general", issues)


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class ManualDatabaseTests(unittest.TestCase):
    def test_manual_provenance_timestamps_publication_and_identical_file_replay(self):
        from election_pipeline import database, publication
        from election_pipeline.parsing import parse_source

        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        db = {**database.connection_settings(), "dbname": name}
        with database.connect(db) as conn:
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                table = {
                    "001_initial.sql": "ops.sources",
                    "002_publication.sql": "serving.publications",
                    "003_state_schemas.sql": "ky.results",
                    "004_mn_ne_tx.sql": "tx.results",
                }.get(path.name, path.stem[4:] + ".results")
                if conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is None:
                    conn.execute(path.read_text())
            conn.execute("TRUNCATE ops.sources CASCADE")
        f = ManualTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        receipt = f.acquire()
        parsed = parse_source(f.source, f.settings, f.data, sha256=receipt["sha256"])
        loaded = database.load_parsed(f.source, f.settings, f.data, Path(parsed["output_dir"]), db)
        self.assertEqual(loaded["outcome"], "succeeded", loaded)
        checked = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        self.assertEqual(checked["outcome"], "validated", checked)
        published = publication.publish_prepared(ROOT, db, checked["candidate_id"])
        self.assertEqual(published["outcome"], "published", published)
        with database.connect(db) as conn:
            row = conn.execute(
                "SELECT http_status,snapshot_id,metadata FROM ops.fetch_attempts "
                "WHERE outcome='manual_imported'"
            ).fetchone()
            self.assertIsNone(row["http_status"])
            self.assertIsNotNone(row["snapshot_id"])
            self.assertEqual(row["metadata"]["original_sha256"], receipt["original_sha256"])
            metadata = conn.execute(
                "SELECT first_observed_at,source_modified_at FROM serving.publications "
                "WHERE publication_id=%s",
                (published["publication_id"],),
            ).fetchone()
            self.assertEqual(metadata["first_observed_at"], datetime.fromisoformat(f.observed))
            self.assertEqual(
                metadata["source_modified_at"],
                datetime.fromisoformat(receipt["source_generated_at"]),
            )
        f.acquire(downloaded_at="2026-11-04T05:01:00+00:00")
        repeated = database.load_parsed(
            f.source, f.settings, f.data, Path(parsed["output_dir"]), db
        )
        self.assertEqual(repeated["release_id"], loaded["release_id"])
        self.assertEqual(
            publication.publish(ROOT, db, loaded["release_id"], "unofficial", skip_current=True)[
                "outcome"
            ],
            "already_current",
        )


if __name__ == "__main__":
    unittest.main()
