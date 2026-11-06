"""Stage additional races beside live sources, then switch their readers together."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
import uuid
from contextlib import ExitStack, suppress
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.deployment import catalog, containers, control, dashboard
from election_pipeline.settings import CollectorSettings
from election_pipeline.states import STATES as REGISTERED
from election_pipeline.storage import ArchiveStore, atomic_json, utc_now

# States with a full-coverage source contract; see State.coverage_seats.
STATES = {code for code, state in REGISTERED.items() if state.coverage_seats}


def names(states):
    if not states or len(states) != len(set(states)) or not set(states) <= STATES:
        raise ValueError("Choose distinct supported states: " + " ".join(sorted(STATES)))
    return [code.lower() + "_2026_general_all" for code in states]


def progress(stage, **fields):
    print(json.dumps({"stage": stage, **fields}), flush=True)


def wait_for_controls(config):
    """Let already-started older controllers finish before adding the new catalog fields."""
    control.controls_ready(config)
    loaded = containers.command(
        ["systemctl", "show", "--value", "-p", "ExecStart", "election-expansion.service"]
    )
    if str(Path(config["CONTROL_ROOT"]) / "scripts/deploy.py") not in loaded:
        raise ValueError("Install the versioned expansion service before preparing coverage")
    started = last_notice = time.monotonic()
    deadline = started + int(config.get("CYCLE_SECONDS", "270")) + 30
    while True:
        busy = []
        for unit in (
            "election-expansion.service",
            "election-monitor.service",
            "election-backup.service",
        ):
            state = containers.command(["systemctl", "show", "--value", "-p", "ActiveState", unit])
            if state not in ("inactive", "failed"):
                busy.append(unit)
        if not busy:
            return
        now = time.monotonic()
        if now >= deadline:
            raise ValueError("Existing control jobs are still running; retry preparation when idle")
        if last_notice == started or now - last_notice >= 30:
            progress("waiting_for_existing_controls", services=busy)
            last_notice = now
        time.sleep(2)


def prepare(path, states, release, image, prod, config):
    """Existing source IDs and current publications remain in use throughout preparation."""
    selected = names(states)
    identity, revision = containers.verify_release(release, image)
    wait_for_controls(config)
    for name in selected:
        value = catalog.read(path)
        old_name = name.removesuffix("_all")
        if name not in value["sources"]:
            old = value["sources"][old_name]
            control.register(
                path, release, identity, name, old["status"], prod, config, replaces=old_name
            )
        else:
            entry = value["sources"][name]
            if entry["enabled"] or entry["public"]:
                raise ValueError("This expanded source is already active")
            if entry.get("replaces") != old_name:
                raise ValueError("Existing source has a different replacement target")
            if entry["image_id"] != identity or entry["revision"] != revision:
                repin_private(path, release, identity, revision, name, prod)
        progress("prepare", source_id=name)
        result = control.prepare(path, name, prod, config)
        progress("validated", source_id=name, release_id=result["release_id"])
        result = control.promote(path, name, prod, config)
        progress("private_publication", source_id=name, publication_id=result["publication_id"])
    return {"outcome": "prepared", "sources": selected, "public_coverage_changed": False}


def repin_private(path, release, identity, revision, name, prod):
    """Replace an inactive preparation image while keeping live sources untouched."""
    before = catalog.read(path)["sources"][name]
    if (
        before["worker"] != "expansion"
        or before["enabled"]
        or before["public"]
        or not before.get("replaces")
    ):
        raise ValueError("Only a disabled, private coverage replacement may be re-pinned")
    meta = containers.metadata(identity, name)
    if meta["code_sha256"] != containers.package_hash(release):
        raise ValueError("Image Python sources differ from the release checkout")
    if (
        catalog.source_from_json(meta["source"]) != catalog.source_from_json(before["source"])
        or catalog.contract_settings(asdict(CollectorSettings(**meta["settings"])))
        != catalog.contract_settings(asdict(CollectorSettings(**before["settings"])))
        or meta["races"] != before["races"]
    ):
        raise ValueError("A private re-pin cannot change source, settings or race coverage")
    with control.connect(control.db_settings(prod, writer=True)) as conn:
        for migration in conn.execute("SELECT name, sha256 FROM ops.schema_migrations").fetchall():
            file = Path(release) / "migrations" / migration["name"]
            if (
                not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != migration["sha256"]
            ):
                raise ValueError("Corrected release changed or omitted an applied migration")
    fingerprint = containers.release_hash(release)

    def change(value):
        catalog.verify_baseline(value, prod)
        entry = value["sources"][name]
        if entry != before:
            raise ValueError("Private source changed during re-pin; retry preparation")
        entry["previous_preparation"] = {
            key: before[key]
            for key in (
                "image_id",
                "revision",
                "release_path",
                "release_sha256",
                "candidate_id",
                "publication_id",
                "published_at",
            )
            if key in before
        }
        entry.update(
            image_id=identity,
            revision=revision,
            release_path=str(Path(release).resolve()),
            release_sha256=fingerprint,
            settings=asdict(CollectorSettings(**meta["settings"])),
        )
        # Keep historical DB publications; require fresh validation for this image.
        for key in ("candidate_id", "publication_id", "published_at"):
            entry.pop(key, None)

    catalog.update(path, change)
    progress("repinned_private_source", source_id=name, revision=revision)


def free_port():
    # Account for Docker bindings even on hosts with the userland proxy disabled.
    published = containers.command(["docker", "ps", "--format", "{{.Ports}}"])
    reserved = {int(port) for port in re.findall(r":([0-9]+)->", published)}
    for port in range(8001, 8100):
        if port in reserved:
            continue
        try:
            with socket.socket() as handle:
                handle.bind(("127.0.0.1", port))
        except OSError:
            continue
        return port
    raise ValueError("No free localhost dashboard port in 8001–8099")


def activate(path, states, prod, config):
    selected = names(states)
    value = catalog.read(path)
    if any(not value["sources"][name].get("replaces") for name in selected):
        raise ValueError("Prepare every expanded source before activation")
    return activate_sources(path, selected, prod, config)


def activate_sources(path, selected, prod, config):
    """Activate newly admitted sources through the existing restore and route gates."""
    if not selected or len(selected) != len(set(selected)):
        raise ValueError("Select distinct registered expansion sources")
    value = catalog.read(path)
    if any(name not in value["sources"] for name in selected):
        raise ValueError("Register every selected source before activation")
    entries = [value["sources"][name] for name in selected]
    if any(entry["worker"] != "expansion" for entry in entries):
        raise ValueError("Activation accepts only registered expansion workers")
    if all(entry["enabled"] and entry["public"] for entry in entries):
        return {"outcome": "already_active", "sources": selected}
    if any(not entry.get("publication_id") for entry in entries):
        raise ValueError("Prepare every expanded source before activation")
    images = {entry["image_id"] for entry in entries}
    if len(images) != 1:
        raise ValueError("Expanded sources must use one checked dashboard image")
    root = Path(config["CONTROL_ROOT"])
    progress("backup")
    containers.command(["systemctl", "start", "election-backup.service"], timeout=1200)
    receipt = json.loads(
        (
            Path(config.get("BACKUP_DIR", "/srv/election-pipeline/backups")) / "latest-backup.json"
        ).read_text(encoding="utf-8")
    )
    if receipt.get("outcome") != "uploaded":
        raise ValueError("The backup did not upload successfully")
    progress("restore", backup_id=receipt["backup_id"])
    restored = json.loads(
        containers.command(
            [
                str(root / ".venv/bin/python"),
                str(root / "scripts/production_recovery.py"),
                "check-restore",
                "--backup-id",
                receipt["backup_id"],
                "--catalog-file",
                str(path),
            ],
            timeout=1200,
        )
    )
    if restored.get("outcome") != "passed":
        raise ValueError("Expanded sources did not pass isolated restore verification")
    for entry in entries:
        control.recovery_gate(entry, restored, datetime.now(timezone.utc))
    progress("restore_passed", report_path=restored["report_path"])
    current = catalog.read(path)
    candidate_id, port = uuid.uuid4().hex, free_port()
    state = Path(config["CONTROL_STATE_DIR"])
    progress("start_dashboard", port=port)
    candidate = dashboard.start(
        current,
        selected,
        prod,
        config,
        state / ("dashboard-" + candidate_id),
        port=port,
        image=next(iter(images)),
    )
    candidate["id"] = candidate_id
    atomic_json(state / ("candidate-" + candidate_id + ".json"), candidate)
    expected = control.expected_states(catalog.prospective(current, selected))
    wait_for_dashboard(candidate["url"], expected)
    return control.activate(path, candidate, restored, prod, config)


def wait_for_dashboard(url, expected):
    deadline = time.monotonic() + 30
    while True:
        try:
            dashboard.check(url, expected)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise ValueError("Candidate dashboard did not become ready in 30 seconds") from None
            time.sleep(1)


def retire(path, states, prod, config):
    """Remove replaced sources from future baseline cycles after their public switch."""
    selected = names(states)
    control.controls_ready(config)
    with catalog.locked(path, exclusive=True, nonblocking=True):
        before = catalog.read(path)
        catalog.verify_baseline(before, prod)
        old_names = []
        for name in selected:
            new = before["sources"][name]
            if not new["public"] or not new["enabled"]:
                raise ValueError("Activate expanded coverage before retiring its old source")
            old_name = new.get("replaces")
            old = before["sources"][old_name]
            if old["public"]:
                raise ValueError("The replaced source is still public")
            if old["worker"] == "baseline":
                old_names.append(old_name)
        if not old_names:
            return {"outcome": "already_retired", "sources": selected}
        return retire_baseline(path, before, old_names, config)


def pending_handoff(path):
    return path.exists() and json.loads(path.read_text(encoding="utf-8"))["outcome"] == "pending"


def require_baseline_idle():
    state = containers.command(
        ["systemctl", "show", "--value", "-p", "ActiveState", "election-pipeline.service"]
    )
    if state not in ("inactive", "failed"):
        raise ValueError("Baseline cycle is running; retry retirement after it finishes")


def retire_baseline(path, before, old_names, config):
    """Retire private sources while holding the exclusive catalog lock."""
    prod_file = Path(config.get("PRODUCTION_ENV", "/etc/election-pipeline/production.env"))
    journal = Path(config["CONTROL_STATE_DIR"]) / "source-handoff.json"
    original = prod_file.read_bytes().decode("utf-8")
    actual_prod = dict(dotenv_values(prod_file, interpolate=False))
    catalog.verify_baseline(before, actual_prod)
    if any(before["sources"][name]["public"] for name in old_names):
        raise ValueError("Remove sources from public coverage before retiring them")
    remaining = [name for name in before["baseline"]["source_ids"] if name not in old_names]
    if not remaining:
        raise ValueError("Retiring the last baseline source requires separate worker retirement")
    changed, count = re.subn(
        r"(?m)^PIPELINE_SOURCES[ \t]*=[^\r\n]*",
        "PIPELINE_SOURCES=" + ",".join(remaining),
        original,
    )
    if count != 1:
        raise ValueError("Expected exactly one PIPELINE_SOURCES assignment")
    if pending_handoff(journal):
        raise ValueError("Recover the pending source handoff before retiring collectors")

    require_baseline_idle()
    with ExitStack() as stack:
        for name in sorted(old_names):
            source = catalog.source_from_json(before["sources"][name]["source"])
            store = ArchiveStore(Path(actual_prod["DATA_DIR"]), source)
            stack.enter_context(store.lock(stage="pipeline"))
            stack.enter_context(store.lock())
        after = json.loads(json.dumps(before))
        for name in old_names:
            # A dashboard rollback can re-enable this immutable image in the independent worker.
            after["sources"][name].update(worker="expansion", enabled=False, public=False)
        after["baseline"]["source_ids"] = remaining
        after["generation"] += 1
        catalog.validate(after)
        backup = prod_file.with_name(prod_file.name + ".before-source-" + uuid.uuid4().hex)
        control.private_text(backup, original)
        record = {
            "kind": "coverage_retirement",
            "outcome": "pending",
            "started_at": utc_now(),
            "source_ids": old_names,
            "production_env_backup": str(backup),
            "production_env_path": str(prod_file),
            "catalog_before": before,
            "catalog_after": after,
        }
        atomic_json(journal, record)
        if prod_file.read_bytes().decode("utf-8") != original:
            atomic_json(journal, {**record, "outcome": "cancelled"})
            raise ValueError("Production settings changed during retirement")
        try:
            control.private_text(prod_file, changed)
            require_baseline_idle()
            catalog.write(path, after)
            atomic_json(journal, {**record, "outcome": "applied"})
        except Exception:
            catalog.write(path, before)
            control.private_text(prod_file, original)
            atomic_json(journal, {**record, "outcome": "rolled_back"})
            raise
    return {"outcome": "retired", "sources": old_names, "remaining_baseline_sources": remaining}


def disable_baseline(path, name, prod, config):
    """Hide a state's results and retire its sole enabled baseline source."""
    control.controls_ready(config)
    before = catalog.read(path)
    catalog.verify_baseline(before, prod)
    entry = before["sources"].get(name)
    if entry is None or entry["source"]["dataset"] != "election_results":
        raise ValueError("Select a registered results source")
    state_code = entry["source"]["state"]
    if any(
        other_name != name and other["enabled"] and other["source"]["state"] == state_code
        for other_name, other in before["sources"].items()
    ):
        raise ValueError("Another source is still enabled for this state")
    if not entry["enabled"] and not entry["public"] and entry["worker"] != "baseline":
        return {
            "outcome": "already_disabled",
            "source_id": name,
            "remaining_baseline_sources": before["baseline"]["source_ids"],
            "dashboard_states": before["dashboard"]["states"],
        }
    if entry["worker"] != "baseline":
        raise ValueError("Select a baseline results source")
    if len(before["baseline"]["source_ids"]) < 2:
        raise ValueError("Disabling the last baseline source requires separate worker retirement")

    directory = Path(config["CONTROL_STATE_DIR"])
    handoff = directory / "handoff.json"
    if pending_handoff(handoff) or pending_handoff(directory / "source-handoff.json"):
        raise ValueError("Recover the pending handoff before disabling a source")

    hidden = json.loads(json.dumps(before))
    hidden["sources"][name]["public"] = False
    candidate_name = None
    switched = False
    try:
        if entry["public"]:
            identity = uuid.uuid4().hex
            candidate_name = "election-expansion-dashboard-" + identity
            active_image = before["dashboard"].get("image_id") or before["baseline"]["image_id"]
            candidate = dashboard.start(
                hidden,
                [],
                prod,
                config,
                directory / ("dashboard-" + identity),
                port=free_port(),
                image=active_image,
            )
            wait_for_dashboard(candidate["url"], control.expected_states(hidden))
            hidden["dashboard"] = {**candidate, "id": identity}
            # The prior dashboard includes the removed state and its enabled flags.
            hidden.pop("previous_dashboard", None)
            hidden["generation"] += 1

        with catalog.locked(path, exclusive=True, nonblocking=True):
            if catalog.read(path) != before:
                raise ValueError("Catalog changed; retry disabling the source")
            if pending_handoff(handoff) or pending_handoff(directory / "source-handoff.json"):
                raise ValueError("Recover the pending handoff before disabling a source")
            control.require_no_backup()
            require_baseline_idle()
            if candidate_name:
                control.switch_dashboard(path, before, hidden, candidate_name, config)
                switched = True
                progress("dashboard_switched", states=hidden["dashboard"]["states"])
            try:
                result = retire_baseline(path, hidden, [name], config)
            except Exception as error:
                raise ValueError(
                    f"{name} is off the dashboard; collector retirement did not finish. "
                    f"Retry when the baseline worker is idle. {error}"
                ) from error
    except Exception:
        if candidate_name and not switched and not pending_handoff(handoff):
            with suppress(OSError, subprocess.SubprocessError, ValueError):
                dashboard.stop_unused(candidate_name, catalog.read(path))
        raise
    return {
        **result,
        "outcome": "disabled",
        "source_id": name,
        "dashboard_states": hidden["dashboard"]["states"],
    }


def recover_retirement(path, config):
    """Undo only an interrupted retirement, preserving the expanded public dashboard."""
    journal = Path(config["CONTROL_STATE_DIR"]) / "source-handoff.json"
    with catalog.locked(path, exclusive=True, nonblocking=True):
        record = json.loads(journal.read_text(encoding="utf-8"))
        if record.get("kind") != "coverage_retirement" or record.get("outcome") != "pending":
            raise ValueError("No interrupted coverage retirement to recover")
        current = catalog.read(path)
        if current not in (record["catalog_before"], record["catalog_after"]):
            raise ValueError("Catalog changed after the interruption; inspect it before recovery")
        prod_file = Path(config.get("PRODUCTION_ENV", "/etc/election-pipeline/production.env"))
        if str(prod_file) != record["production_env_path"]:
            raise ValueError("Recovery refers to another production configuration")
        require_baseline_idle()
        original = Path(record["production_env_backup"]).read_bytes().decode("utf-8")
        changed = re.sub(
            r"(?m)^PIPELINE_SOURCES[ \t]*=[^\r\n]*",
            "PIPELINE_SOURCES=" + ",".join(record["catalog_after"]["baseline"]["source_ids"]),
            original,
        )
        if prod_file.read_bytes().decode("utf-8") not in (original, changed):
            raise ValueError("Production configuration changed after the interruption")
        control.private_text(prod_file, original)
        catalog.write(path, record["catalog_before"])
        atomic_json(journal, {**record, "outcome": "rolled_back"})
    return {"outcome": "recovered", "note": "Public dashboard and expanded sources were preserved"}
