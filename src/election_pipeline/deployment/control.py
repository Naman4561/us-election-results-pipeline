"""Host-side release control. Existing collector units and tags stay untouched."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.database import connect
from election_pipeline.deployment import catalog, containers, dashboard, migrations
from election_pipeline.settings import CollectorSettings, load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, atomic_json, file_hash, utc_now

GOOD = {"succeeded", "already_current", "not_due", "awaiting_publication"}


def db_settings(prod, *, writer=False):
    return {
        "host": prod.get("POSTGRES_HOST", "127.0.0.1"),
        "port": int(prod.get("POSTGRES_PORT", "5432")),
        "dbname": prod["POSTGRES_DB"],
        "user": prod["PIPELINE_DB_USER" if writer else "POSTGRES_USER"],
        "password": prod["PIPELINE_DB_PASSWORD" if writer else "POSTGRES_PASSWORD"],
        "connect_timeout": 5,
    }


def expansion(value, name):
    entry = value["sources"][name]
    if entry["worker"] != "expansion":
        raise ValueError("This action is restricted to new expansion sources")
    return entry


def import_county(path, name, county, report, review, prod, config):
    with catalog.locked(path):
        entry = expansion(catalog.read(path), name)
        source = catalog.source_from_json(entry["source"])
        from election_pipeline.county_import import selected

        if source.state != "MS" or not entry["enabled"] or not entry["public"]:
            raise ValueError("Select the active public Mississippi expansion source")
        selected(source, county)
        return json.loads(containers.import_county(entry, prod, config, county, report, review))


def controls_ready(config):
    root = config["CONTROL_ROOT"]
    for unit, script in (
        ("election-backup.service", "production_recovery.py"),
        ("election-monitor.service", "monitor_production.py"),
    ):
        loaded = containers.command(["systemctl", "show", "--value", "-p", "ExecStart", unit])
        if (
            str(Path(root) / "scripts" / script) not in loaded
            or config["CATALOG_FILE"] not in loaded
        ):
            raise ValueError("Install versioned backup/monitor controls before registering states")


def initialize(path, root, prod):
    root = Path(root).resolve()
    image_id, revision = containers.inspect_image("election-pipeline:" + prod["IMAGE_TAG"])
    if (
        revision != prod["IMAGE_TAG"]
        or containers.command(["git", "-C", str(root), "rev-parse", "HEAD"]) != revision
    ):
        raise ValueError("Baseline checkout, image and IMAGE_TAG must agree")
    if containers.command(["git", "-C", str(root), "status", "--porcelain"]):
        raise ValueError("Record a clean baseline before installing deployment controls")
    code_hash = containers.command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            image_id,
            "python",
            "-c",
            "from election_pipeline.pipeline import collector_code_hash; "
            "print(collector_code_hash())",
        ]
    )
    if code_hash != containers.package_hash(root):
        raise ValueError("Baseline image and checkout Python sources differ")
    release_hash = containers.release_hash(root)
    settings, sources = load_config(root / "config/sources.toml")
    names = [name.strip() for name in prod["PIPELINE_SOURCES"].split(",") if name.strip()]
    if not names or len(names) != len(set(names)) or not set(names) <= sources.keys():
        raise ValueError("Invalid baseline source list")
    value = {
        "version": 1,
        "generation": 0,
        "baseline": {"image_id": image_id, "revision": revision, "source_ids": names},
        "sources": {},
        "dashboard": {
            "name": "dashboard",
            "url": "http://127.0.0.1:8000",
            "states": sorted(sources[n].state for n in names),
        },
    }
    for name, source in sources.items():
        if source.contests and name not in names:
            continue
        value["sources"][name] = catalog.entry(
            source,
            settings,
            image_id=image_id,
            revision=revision,
            release_path=root,
            worker="baseline" if name in names else "manual",
            status=prod.get("PIPELINE_STATUS", "test"),
        )
        value["sources"][name]["races"] = list(state_adapter(source.state).races)
        value["sources"][name]["release_sha256"] = release_hash
    with catalog.locked(path, exclusive=True):
        if Path(path).exists():
            raise ValueError("Catalog already exists; refusing to replace it")
        catalog.write(path, value)
    return {"outcome": "initialized", "baseline_sources": names, "image_id": image_id}


def install_controls(config, unit_root=Path("/etc/systemd/system")):
    root, path = Path(config["CONTROL_ROOT"]), config["CATALOG_FILE"]
    for item in (str(root), path):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", item):
            raise ValueError("Use simple absolute release/catalog paths for systemd")
    if not (root / ".venv/bin/python").is_file():
        raise ValueError(
            "Create the control release's venv and install its deployment extras first"
        )
    for unit, action in (
        ("election-backup.service", "production_recovery.py backup"),
        ("election-monitor.service", "monitor_production.py"),
    ):
        directory = unit_root / (unit + ".d")
        directory.mkdir(parents=True, exist_ok=True)
        text = (
            f"[Service]\nWorkingDirectory={root}\nExecStart=\n"
            f"ExecStart={root}/.venv/bin/python {root}/scripts/{action} --catalog-file {path}\n"
        )
        if unit == "election-backup.service":
            text += (
                f"ExecStartPost=\nExecStartPost={root}/.venv/bin/python "
                f"{root}/scripts/prune_backups.py --keep 3 --apply\n"
            )
        (directory / "deployment.conf").write_text(text, encoding="utf-8")
    for unit in ("election-expansion.service", "election-expansion.timer"):
        template = (root / "deploy/systemd" / unit).read_text(encoding="utf-8")
        (unit_root / unit).write_text(
            template.replace("@CONTROL_ROOT@", str(root)), encoding="utf-8"
        )
    containers.command(["systemctl", "daemon-reload"])
    # No stop/restart/start of any existing unit. The next invocation uses the new code.
    return {"outcome": "installed", "note": "Existing running jobs were left untouched"}


def register(path, release, image, name, status, prod, config, *, replaces=None):
    controls_ready(config)
    identity, revision = containers.verify_release(release, image)
    metadata = containers.metadata(identity, name)
    if metadata["code_sha256"] != containers.package_hash(release):
        raise ValueError("Image Python sources differ from its release checkout")
    source = catalog.source_from_json(metadata["source"])
    settings = CollectorSettings(**metadata["settings"])
    with connect(db_settings(prod, writer=True)) as conn:
        for migration in conn.execute("SELECT name, sha256 FROM ops.schema_migrations").fetchall():
            file = Path(release) / "migrations" / migration["name"]
            if (
                not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != migration["sha256"]
            ):
                raise ValueError("Candidate release changed or omitted an applied migration")
    entry = catalog.entry(
        source,
        settings,
        image_id=identity,
        revision=revision,
        release_path=release,
        worker="expansion",
        status=status,
    )
    entry["races"] = metadata["races"]
    entry["release_sha256"] = containers.release_hash(release)
    if replaces:
        entry["replaces"] = replaces

    def change(value):
        catalog.verify_baseline(value, prod)
        if name in value["sources"]:
            raise ValueError("Source/state is already owned; existing states cannot be replaced")
        existing = [
            (n, e)
            for n, e in value["sources"].items()
            if e["source"]["state"] == source.state and e["source"]["dataset"] == "election_results"
        ]
        if replaces:
            if len(existing) != 1 or existing[0][0] != replaces:
                raise ValueError("Replacement must name the state's one existing results source")
            old = catalog.source_from_json(existing[0][1]["source"])
            if (
                any(
                    getattr(old, k) != getattr(source, k)
                    for k in ("state", "election_date", "dataset", "format")
                )
                or not source.contests
                or not catalog.expands_coverage(existing[0][1], entry)
                or status != existing[0][1]["status"]
            ):
                raise ValueError("Replacement must preserve election/status and admitted coverage")
        elif existing:
            raise ValueError("Source/state is already owned; use an explicit coverage replacement")
        # A legacy backup already in flight may not hold the new catalog lock.
        active = containers.command(
            ["systemctl", "show", "--value", "-p", "ActiveState", "election-backup.service"]
        )
        if active in ("active", "activating", "deactivating", "reloading"):
            raise ValueError("A backup is running; retry registration after it finishes")
        lock_dir = Path(prod["DATA_DIR"]) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            for suffix in (".lock", ".pipeline.lock"):
                lock = lock_dir / (name + suffix)
                # Refuse to adopt old source state or change another owner's permissions.
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created.append(lock)
                try:
                    os.fchown(descriptor, 10001, 10001)
                finally:
                    os.close(descriptor)
        except Exception:
            for lock in created:
                lock.unlink()
            raise
        value["sources"][name] = entry

    catalog.update(path, change)
    return {"outcome": "registered", "source_id": name, "enabled": False, "public": False}


def private_text(path, text):
    """Replace one private configuration file without exposing its contents in argv."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".source-handoff-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def transfer_source(path, release, image, name, status, evidence, report_file, config):
    """Move one existing source to the independent worker; leave other images pinned."""
    controls_ready(config)
    containers.limits(config)
    identity, revision = containers.verify_release(release, image)
    meta = containers.metadata(identity, name)
    if meta["code_sha256"] != containers.package_hash(release):
        raise ValueError("Image sources differ from the release checkout")
    source = catalog.source_from_json(meta["source"])
    settings = CollectorSettings(**meta["settings"])
    report = json.loads(Path(report_file).read_text(encoding="utf-8"))
    checked_at = datetime.fromisoformat(report["checked_at"])
    if not 0 <= (datetime.now(timezone.utc) - checked_at).total_seconds() <= 1800:
        raise ValueError("Run the saved-response parser check within 30 minutes of transfer")
    if any(
        (
            report.get("outcome") != "parsed",
            report.get("source_id") != name,
            report.get("source_signature") != source.signature(settings),
            report.get("collector_code_sha256") != meta["code_sha256"],
            report.get("raw_sha256") != file_hash(Path(evidence)),
            report.get("internal_reconciliation") != "passed",
            report.get("contest_total_reconciliation") != "passed",
            report.get("contest_count") != len(meta["races"]),
            report.get("all_counts_zero") is not False,
        )
    ):
        raise ValueError("Transfer requires a matching successful nonzero saved-response check")
    new = catalog.entry(
        source,
        settings,
        image_id=identity,
        revision=revision,
        release_path=release,
        worker="expansion",
        status=status,
    )
    new.update(races=meta["races"], release_sha256=containers.release_hash(release))
    prod_file = Path(config.get("PRODUCTION_ENV", "/etc/election-pipeline/production.env"))
    journal = Path(config["CONTROL_STATE_DIR"]) / "source-handoff.json"
    with catalog.locked(path, exclusive=True, nonblocking=True):
        before = catalog.read(path)
        old = before["sources"][name]
        if old["worker"] != "baseline":
            raise ValueError("Only a currently baseline-owned source may be transferred")
        old_source = catalog.source_from_json(old["source"])
        if (
            name != source.source_id
            or any(
                getattr(source, key) != getattr(old_source, key)
                for key in ("state", "election_date", "dataset")
            )
            or source.dataset != "election_results"
            or meta["races"] != old["races"]
        ):
            raise ValueError("Transfer cannot change source identity or watched race coverage")
        original = prod_file.read_bytes().decode("utf-8")
        prod = dict(dotenv_values(prod_file, interpolate=False))
        catalog.verify_baseline(before, prod)
        remaining = [
            item.strip()
            for item in prod["PIPELINE_SOURCES"].split(",")
            if item.strip() and item.strip() != name
        ]
        if not remaining:
            raise ValueError("Retiring the last baseline source needs a separate worker retirement")
        changed, matches = re.subn(
            r"(?m)^PIPELINE_SOURCES[ \t]*=[^\r\n]*",
            "PIPELINE_SOURCES=" + ",".join(remaining),
            original,
        )
        if matches != 1:
            raise ValueError("Expected exactly one PIPELINE_SOURCES assignment")
        if (
            journal.exists()
            and json.loads(journal.read_text(encoding="utf-8"))["outcome"] == "pending"
        ):
            raise ValueError("A previous source handoff needs inspection before another transfer")

        def require_idle():
            state = containers.command(
                ["systemctl", "show", "--value", "-p", "ActiveState", "election-pipeline.service"]
            )
            if state not in ("inactive", "failed"):
                raise ValueError("The baseline cycle is running; retry transfer after it finishes")

        require_idle()
        with (
            ArchiveStore(Path(prod["DATA_DIR"]), old_source).lock(stage="pipeline"),
            ArchiveStore(Path(prod["DATA_DIR"]), old_source).lock(),
        ):
            evidence_dir = Path(prod["DATA_DIR"]) / "accuracy/adapter_checks" / name
            evidence_dir.mkdir(parents=True, exist_ok=True)
            saved = evidence_dir / (report["raw_sha256"] + ".json")
            saved.write_bytes(Path(evidence).read_bytes())
            atomic_json(evidence_dir / (report["raw_sha256"] + ".check.json"), report)
            after = json.loads(json.dumps(before))
            new.update(enabled=old["enabled"], public=old["public"], previous_release=old)
            after["sources"][name] = new
            after["baseline"]["source_ids"] = remaining
            after["generation"] += 1
            catalog.validate(after)
            backup = prod_file.with_name(prod_file.name + ".before-source-" + uuid.uuid4().hex)
            private_text(backup, original)
            record = {
                "outcome": "pending",
                "source_id": name,
                "started_at": utc_now(),
                "production_env_backup": str(backup),
                "catalog_before": before,
                "catalog_after": after,
            }
            atomic_json(journal, record)
            if prod_file.read_bytes().decode("utf-8") != original:
                atomic_json(journal, {**record, "outcome": "cancelled"})
                raise ValueError("Production configuration changed during transfer; retry")
            try:
                private_text(prod_file, changed)
                require_idle()
                catalog.write(path, after)
                atomic_json(journal, {**record, "outcome": "applied"})
            except Exception:
                catalog.write(path, before)
                private_text(prod_file, original)
                atomic_json(journal, {**record, "outcome": "rolled_back"})
                raise
    return {
        "outcome": "transferred",
        "source_id": name,
        "worker": "expansion",
        "remaining_baseline_sources": remaining,
        "image_id": identity,
        "note": "The independent worker performs its normal validation before publication",
    }


def run_one(entry, prod, config, *, prepare=False):
    args = [
        "scripts/run_expansion_source.py",
        "--source",
        entry["source"]["source_id"],
        "--status",
        entry["status"],
    ]
    if prepare:
        args.append("--prepare-only")
    output = containers.run_job(
        entry, prod, config, args, timeout=int(config.get("JOB_TIMEOUT", "240"))
    )
    # run_dbt directs logs to stderr; tolerate separate JSON/log lines from dependencies.
    rows = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    result = rows[-1]
    if result.get("source_id") != entry["source"]["source_id"]:
        raise ValueError("Worker returned the wrong source identity")
    return result


def prepare(path, name, prod, config):
    value = catalog.read(path)
    entry = expansion(value, name)
    if entry["enabled"]:
        raise ValueError("Use the recurring worker for an already enabled source")
    result = run_one(entry, prod, config, prepare=True)
    if result["outcome"] != "validated":
        raise ValueError("The new source did not produce a validated candidate")

    def change(value):
        current = expansion(value, name)
        if current["image_id"] != entry["image_id"] or current["enabled"]:
            raise ValueError("Source deployment changed during preparation")
        current["candidate_id"] = str(uuid.UUID(result["candidate_id"]))

    catalog.update(path, change)
    return result


def promote(path, name, prod, config, *, allow_manual=False):
    value = catalog.read(path)
    entry = value["sources"][name]
    if entry["worker"] == "manual" and allow_manual:
        if entry.get("manual_candidate", {}).get("candidate_id") != entry.get("candidate_id"):
            raise ValueError("Manual acquisition receipt does not match the prepared candidate")
    else:
        entry = expansion(value, name)
    candidate = entry["candidate_id"]
    with connect(db_settings(prod, writer=True)) as conn:
        row = conn.execute(
            "SELECT s.source_id FROM ops.publication_attempts a "
            "JOIN ops.processing_releases r USING (release_id) "
            "JOIN ops.snapshots s USING (snapshot_id) WHERE a.attempt_id=%s",
            (candidate,),
        ).fetchone()
        if not row or row["source_id"] != name:
            raise ValueError("Candidate belongs to another source")
    result = json.loads(
        containers.run_job(
            entry, prod, config, ["scripts/publish.py", "promote", "--candidate-id", candidate]
        )
    )
    if result.get("outcome") not in ("published", "skipped", "already_current"):
        raise ValueError("Publication promotion failed")

    def change(value):
        current = value["sources"][name]
        if current.get("candidate_id") != candidate:
            raise ValueError("Candidate changed while being promoted")
        current["published_at"] = utc_now()
        current["publication_id"] = result["publication_id"]
        if entry["worker"] == "manual":
            current["manual_snapshot"] = {
                **current["manual_candidate"],
                "publication_id": result["publication_id"],
            }

    catalog.update(path, change)
    latest = Path(prod["DATA_DIR"]) / "runs" / name / "latest.json"
    run = json.loads(latest.read_text(encoding="utf-8"))
    if run.get("candidate_id") == candidate and run.get("outcome") == "validated":
        run.update(
            outcome="succeeded", publication_id=result["publication_id"], finished_at=utc_now()
        )
        run["stages"].append(
            {"stage": "manual_promotion", "outcome": "published", "result": result}
        )
        atomic_json(latest, run)
        atomic_json(Path(prod["DATA_DIR"]) / "runs" / name / (run["run_id"] + ".json"), run)
    return result


def recovery_gate(entry, report, now):
    if report.get("outcome") != "passed" or not all(
        report.get(key)
        for key in (
            "temporary_database_removed",
            "writer_permissions_verified",
            "reader_permissions_verified",
        )
    ):
        raise ValueError("A successful isolated restore and permission check is required")
    finished = datetime.fromisoformat(report["finished_at"])
    published = datetime.fromisoformat(entry["published_at"])
    if finished < published or not 0 <= (now - finished).total_seconds() <= 1800:
        raise ValueError("Restore check must be recent and after the new state's publication")
    name = entry["source"]["source_id"]
    if report.get("current_publications", {}).get(name) != entry["publication_id"]:
        raise ValueError("Restore check did not verify this state's published snapshot")
    table = entry["source"]["state"].lower() + ".contest_candidate_results"
    if report.get("database_inventory", {}).get(table, {}).get("rows", 0) <= 0:
        raise ValueError("Restored new-state published results are missing")


def expected_states(value):
    return {e["source"]["state"]: e for e in value["sources"].values() if e["public"]}


def switch_dashboard(path, before, after, target, config):
    journal = Path(config["CONTROL_STATE_DIR"]) / "handoff.json"
    if journal.exists() and json.loads(journal.read_text(encoding="utf-8"))["outcome"] == "pending":
        raise ValueError("Recover the pending dashboard handoff first")
    old = dashboard.handoff(
        config["CADDYFILE"],
        config["CADDY_CONTAINER"],
        target + ":8000",
        journal,
        before=before,
        catalog_file=path,
    )
    record = json.loads(journal.read_text(encoding="utf-8"))
    try:
        dashboard.check(config.get("ORIGIN_URL", "http://127.0.0.1"), expected_states(after))
        catalog.write(path, after)
        atomic_json(journal, {**record, "outcome": "applied"})
    except Exception:
        # The journal stays pending if any recovery step fails, for an explicit retry.
        dashboard.reload(config["CADDY_CONTAINER"], old)
        dashboard.persist(config["CADDYFILE"], old)
        catalog.write(path, before)
        atomic_json(journal, {**record, "outcome": "rolled_back"})
        raise
    return old


def activate(path, candidate, report, prod, config):
    controls_ready(config)
    containers.limits(config)
    with catalog.locked(path, exclusive=True):
        value = catalog.read(path)
        catalog.verify_baseline(value, prod)
        if value["generation"] != candidate["catalog_generation"]:
            raise ValueError("Catalog changed; create and check a fresh dashboard candidate")
        if candidate["name"] != "election-expansion-dashboard-" + candidate["id"]:
            raise ValueError("Invalid candidate identity")
        prospective = catalog.prospective(value, candidate["sources"])
        for name in candidate["sources"]:
            entry = prospective["sources"][name]
            if (
                entry["worker"] not in ("expansion", "manual")
                or entry["source"]["dataset"] != "election_results"
            ):
                raise ValueError("Only managed result sources may be activated")
            recovery_gate(entry, report, datetime.now(timezone.utc))
        payload = dashboard.check(candidate["url"], expected_states(prospective))
        for name in candidate["sources"]:
            entry = prospective["sources"][name]
            if (
                str(payload["states"][entry["source"]["state"]]["publication"]["publication_id"])
                != entry["publication_id"]
            ):
                raise ValueError(
                    "The privately checked publication changed; validate and back up again"
                )
        old = Path(config["CADDYFILE"]).read_text(encoding="utf-8")
        prospective["previous_dashboard"] = {
            **value["dashboard"],
            "caddyfile": old,
            "source_flags": {
                name: {key: entry[key] for key in ("enabled", "public")}
                for name, entry in value["sources"].items()
            },
        }
        prospective["dashboard"] = candidate
        prospective["generation"] += 1
        switch_dashboard(path, value, prospective, candidate["name"], config)
    return {"outcome": "activated", "sources": candidate["sources"], "dashboard": candidate["name"]}


def rollback(path, config):
    with catalog.locked(path, exclusive=True):
        before = catalog.read(path)
        value = json.loads(json.dumps(before))
        previous = value["previous_dashboard"]
        for name, entry in value["sources"].items():
            if name in previous.get("source_flags", {}):
                entry.update(previous["source_flags"][name])
            if entry["source"]["state"] not in previous["states"] and entry["worker"] in (
                "expansion",
                "manual",
            ):
                entry.update(public=False, enabled=False)
        desired = {
            code: entry
            for code, entry in expected_states(value).items()
            if code in previous["states"]
        }
        dashboard.check(previous["url"], desired)
        value["dashboard"] = {k: v for k, v in previous.items() if k != "caddyfile"}
        value.pop("previous_dashboard")
        value["generation"] += 1
        switch_dashboard(path, before, value, previous["name"], config)
    return {"outcome": "rolled_back", "dashboard": previous["name"]}


def run_enabled(path, prod, config):
    """One bounded sequential worker; never run or reconfigure baseline sources."""
    value = catalog.read(path)
    catalog.verify_baseline(value, prod)
    containers.limits(config)
    deadline = time.monotonic() + int(config.get("CYCLE_SECONDS", "270"))
    results = []
    entries = [
        (name, e)
        for name, e in value["sources"].items()
        if e["worker"] == "expansion" and e["enabled"]
    ]

    # Start with the least recently attempted source to avoid starving later states.
    def last_run(pair):
        path = Path(prod["DATA_DIR"]) / "runs" / pair[0] / "latest.json"
        return path.stat().st_mtime if path.exists() else 0

    for name, entry in sorted(entries, key=last_run):
        if time.monotonic() >= deadline:
            results.append(
                {"source_id": name, "outcome": "deferred", "error": "Cycle budget reached"}
            )
            continue
        try:
            current = catalog.read(path)["sources"][name]
            if not current["enabled"]:
                continue
            job_config = {
                **config,
                "JOB_TIMEOUT": str(
                    min(
                        int(config.get("JOB_TIMEOUT", "240")),
                        max(1, int(deadline - time.monotonic())),
                    )
                ),
            }
            result = run_one(entry, prod, job_config)
        except Exception as error:
            result = {"source_id": name, "outcome": "failed", "error": str(error)}
        encoded = json.dumps(result)
        for key, secret in prod.items():
            if "PASSWORD" in key and secret:
                encoded = encoded.replace(secret, "[redacted]")
        result = json.loads(encoded)
        results.append(result)
        print(json.dumps(result), flush=True)
    atomic_json(
        Path(config["CONTROL_STATE_DIR"]) / "expansion-latest.json",
        {
            "finished_at": utc_now(),
            "results": results,
            "outcome": "succeeded" if all(r["outcome"] in GOOD for r in results) else "failed",
        },
    )
    return 0 if all(r["outcome"] in GOOD for r in results) else 1


def migrate_source(path, name, file, prod):
    with catalog.locked(path, exclusive=True):
        entry = expansion(catalog.read(path), name)
        file = Path(file).resolve()
        if file.parent != Path(entry["release_path"]) / "migrations":
            raise ValueError("Migration must be tracked in the registered release")
        containers.command(
            ["git", "-C", entry["release_path"], "ls-files", "--error-unmatch", str(file)]
        )
        if containers.command(["git", "-C", entry["release_path"], "status", "--porcelain"]):
            raise ValueError("Registered release checkout is no longer clean")
        with connect(db_settings(prod, writer=True)) as conn:
            return migrations.apply(conn, file, entry["source"]["state"].lower())
