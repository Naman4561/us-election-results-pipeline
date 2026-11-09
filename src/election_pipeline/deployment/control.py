"""Host-side control of the one release that runs every state.

A release is staged (scripts/stage_release.sh: checkout, image, tests) and then made
current with ``release``: migrations apply, every enabled HTTP source must validate a
publication candidate under the new image, and only then do the catalog and the
``current`` symlink (which the systemd units run from) move. The public dashboard moves
separately, after a candidate dashboard from the release passes its checks.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline.database import connect
from election_pipeline.deployment import catalog, containers, dashboard
from election_pipeline.migrations import migrate
from election_pipeline.storage import atomic_json, utc_now
from election_pipeline.workflow import NO_SNAPSHOT

# A locked source is held by another run or a backup; it is not a failure.
GOOD = {"succeeded", "already_current", "not_due", "awaiting_publication", "locked"}


def db_settings(prod, *, writer=False):
    return {
        "host": prod.get("POSTGRES_HOST", "127.0.0.1"),
        "port": int(prod.get("POSTGRES_PORT", "5432")),
        "dbname": prod["POSTGRES_DB"],
        "user": prod["PIPELINE_DB_USER" if writer else "POSTGRES_USER"],
        "password": prod["PIPELINE_DB_PASSWORD" if writer else "POSTGRES_PASSWORD"],
        "connect_timeout": 5,
    }


def redacted(text, prod):
    for key, secret in prod.items():
        if "PASSWORD" in key and secret:
            text = text.replace(secret, "[redacted]")
    return text


def require_no_backup():
    active = containers.command(
        ["systemctl", "show", "--value", "-p", "ActiveState", "election-backup.service"]
    )
    if active in ("active", "activating", "deactivating", "reloading"):
        raise ValueError("A backup is running; retry after it finishes")


# Jobs ---------------------------------------------------------------------------------


def run_one(image, name, status, prod, config, *, mode=None):
    """Run one source in the release image; ``mode`` is prepare-only or check-only."""
    args = ["scripts/run_source.py", "--source", name, "--status", status]
    if mode:
        args.append("--" + mode)
    output = containers.run_job(
        image, prod, config, args, timeout=int(config.get("JOB_TIMEOUT", "240"))
    )
    # run_dbt directs logs to stderr; tolerate separate JSON/log lines from dependencies.
    result = [json.loads(line) for line in output.splitlines() if line.startswith("{")][-1]
    if result.get("source_id") != name:
        raise ValueError("Worker returned the wrong source identity")
    return result


def parallel_jobs(config):
    value = config.get("JOB_PARALLEL", "1")
    if not re.fullmatch(r"[1-4]", value):
        raise ValueError("Set JOB_PARALLEL to 1-4 jobs after checking host headroom")
    return int(value)


def collected(value):
    return sorted(
        name
        for name, entry in value["sources"].items()
        if entry["enabled"] and entry["acquisition"] == "http"
    )


def run_enabled(path, prod, config):
    """One bounded cycle over every enabled HTTP source, least recently run first."""
    value = catalog.read(path)
    containers.limits(config)
    deadline = time.monotonic() + int(config.get("CYCLE_SECONDS", "270"))

    def last_run(name):
        latest = Path(prod["DATA_DIR"]) / "runs" / name / "latest.json"
        return latest.stat().st_mtime if latest.exists() else 0

    def job(name):
        if time.monotonic() >= deadline:
            return {"source_id": name, "outcome": "deferred", "error": "Cycle budget reached"}
        try:
            current = catalog.read(path)
            entry = current["sources"][name]
            if not entry["enabled"]:
                return None
            remaining = max(1, int(deadline - time.monotonic()))
            budget = {
                **config,
                "JOB_TIMEOUT": str(min(int(config.get("JOB_TIMEOUT", "240")), remaining)),
            }
            result = run_one(current["release"]["image_id"], name, entry["status"], prod, budget)
        except Exception as error:
            result = {"source_id": name, "outcome": "failed", "error": str(error)}
        encoded = redacted(json.dumps(result), prod)
        print(encoded, flush=True)
        return json.loads(encoded)

    # Each job has its own container limits, so N parallel jobs use N times the budget.
    with ThreadPoolExecutor(max_workers=parallel_jobs(config)) as pool:
        results = [
            r for r in pool.map(job, sorted(collected(value), key=last_run)) if r is not None
        ]
    ok = all(r["outcome"] in GOOD for r in results)
    atomic_json(
        Path(config["CONTROL_STATE_DIR"]) / "worker-latest.json",
        {"finished_at": utc_now(), "results": results, "outcome": "succeeded" if ok else "failed"},
    )
    return 0 if ok else 1


# Releases -----------------------------------------------------------------------------


def describe(release_path, image):
    """The release record for a staged checkout and the image built from it."""
    identity, revision = containers.verify_release(release_path, image)
    code_hash = containers.command(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            identity,
            "python",
            "-c",
            "from election_pipeline.pipeline import collector_code_hash; "
            "print(collector_code_hash())",
        ]
    )
    if code_hash != containers.package_hash(release_path):
        raise ValueError("Image Python sources differ from the release checkout")
    return {
        "revision": revision,
        "image_id": identity,
        "release_path": str(Path(release_path).resolve()),
    }


def point_current(config, release_path):
    """Atomically repoint the ``current`` symlink the systemd units run from."""
    link = Path(config["CURRENT_RELEASE"])
    temporary = link.with_name(".current-" + uuid.uuid4().hex)
    os.symlink(release_path, temporary)
    os.replace(temporary, link)


def check_sources(image, value, prod, config, moved=()):
    """Run every enabled HTTP source once, check-only, in ``image``; return those that failed.

    Each run parses the source's latest archived snapshot, loads it and builds dbt, then
    discards its candidate. Nothing is requested from the state, so a site that is down
    does not block a release, and the live collector state is left alone.

    A source in ``moved`` now reads a new address, so it has no snapshot to check yet;
    for it alone, having none is not a failure. Its first collection is validated before
    it publishes, like any other.
    """
    unknown = set(moved) - set(collected(value))
    if unknown:
        raise ValueError(f"Not an enabled HTTP source: {sorted(unknown)}")

    def check(name):
        try:
            result = run_one(
                image, name, value["sources"][name]["status"], prod, config, mode="check-only"
            )
        except Exception as error:
            result = {"source_id": name, "outcome": "failed", "error": str(error)}
        return name, result

    with ThreadPoolExecutor(max_workers=parallel_jobs(config)) as pool:
        checks = dict(pool.map(check, collected(value)))
    return {
        n: r.get("error") or r["outcome"]
        for n, r in checks.items()
        if r["outcome"] != "validated" and not (n in moved and no_snapshot(r.get("error")))
    }


def no_snapshot(error):
    """Whether a failed check said only that the source has no snapshot to check.

    A job that fails exits nonzero, so its JSON report, the last JSON line it prints,
    reaches us inside the runner's "Command failed" message rather than as the result.
    """
    for line in reversed((error or "").splitlines()):
        start = line.find('{"')
        if start >= 0:
            try:
                return json.loads(line[start:]).get("error") == NO_SNAPSHOT
            except ValueError:
                return False
    return error == NO_SNAPSHOT


def release(path, release_path, image, prod, config, moved=()):
    """Make a staged release current once it validates every enabled HTTP source."""
    staged = describe(release_path, image)
    value = catalog.read(path)
    if staged["revision"] == value["release"]["revision"]:
        return {"outcome": "already_current", "revision": staged["revision"]}
    with connect(db_settings(prod, writer=True)) as conn:
        applied = migrate(conn, root=staged["release_path"])
    failed = check_sources(staged["image_id"], value, prod, config, moved)
    if failed:
        raise ValueError(redacted("The release did not validate: " + json.dumps(failed), prod))

    def change(current):
        if current["release"] != value["release"]:
            raise ValueError("The release changed during validation; retry")
        require_no_backup()
        current["previous_release"] = current["release"]
        current["release"] = staged

    catalog.update(path, change)
    point_current(config, staged["release_path"])
    return {
        "outcome": "released",
        "revision": staged["revision"],
        "previous_revision": value["release"]["revision"],
        "migrations_applied": applied,
        "sources_validated": collected(value),
        "note": "Workers use the release from their next cycle; move the dashboard next",
    }


def rollback_release(path, config):
    """Return workers to the previous release. Migrations are additive and stay applied."""

    def change(value):
        if value.get("previous_release") is None:
            raise ValueError("No previous release is recorded")
        require_no_backup()
        value["release"], value["previous_release"] = value["previous_release"], value["release"]

    value = catalog.update(path, change)
    point_current(config, value["release"]["release_path"])
    return {"outcome": "rolled_back", "revision": value["release"]["revision"]}


# New sources --------------------------------------------------------------------------


def prepare(path, name, prod, config):
    """Validate a not-yet-enabled source's first publication candidate."""
    value = catalog.read(path)
    entry = value["sources"][name]
    if entry["enabled"] or entry["acquisition"] != "http":
        raise ValueError("Prepare a disabled HTTP source; enabled sources publish on their own")
    result = run_one(
        value["release"]["image_id"], name, entry["status"], prod, config, mode="prepare-only"
    )
    if result["outcome"] != "validated":
        raise ValueError("The source did not produce a validated candidate")

    def change(current):
        if current["sources"][name]["enabled"]:
            raise ValueError("Source deployment changed during preparation")
        current["sources"][name]["candidate_id"] = str(uuid.UUID(result["candidate_id"]))

    catalog.update(path, change)
    return result


def promote(path, name, prod, config):
    """Publish a source's prepared candidate (still private until dashboard activation)."""
    value = catalog.read(path)
    entry = value["sources"][name]
    if entry["acquisition"] == "manual" and entry.get("manual_candidate", {}).get(
        "candidate_id"
    ) != entry.get("candidate_id"):
        raise ValueError("Manual acquisition receipt does not match the prepared candidate")
    candidate = entry.get("candidate_id")
    if not candidate:
        raise ValueError("Prepare the source first")
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
            value["release"]["image_id"],
            prod,
            config,
            ["scripts/publish.py", "promote", "--candidate-id", candidate],
        )
    )
    if result.get("outcome") not in ("published", "skipped", "already_current"):
        raise ValueError("Publication promotion failed")

    def change(current):
        source = current["sources"][name]
        if source.get("candidate_id") != candidate:
            raise ValueError("Candidate changed while being promoted")
        source.update(published_at=utc_now(), publication_id=result["publication_id"])
        if source["acquisition"] == "manual":
            source["manual_snapshot"] = {
                **source.pop("manual_candidate"),
                "publication_id": result["publication_id"],
            }
        source.pop("candidate_id")

    catalog.update(path, change)
    return result


def disable(path, name):
    """Stop collecting a private source. Public sources leave through a dashboard switch."""

    def change(value):
        entry = value["sources"][name]
        if entry["public"]:
            raise ValueError("Activate a dashboard without this state before disabling it")
        entry["enabled"] = False

    catalog.update(path, change)
    return {"outcome": "disabled", "source_id": name}


def import_county(path, county, report, review, prod, config):
    from election_pipeline.county_import import selected
    from election_pipeline.states import STATES

    name = STATES["MS"].source_id
    with catalog.locked(path):
        value = catalog.read(path)
        entry = value["sources"][name]
        if not entry["enabled"] or not entry["public"]:
            raise ValueError("Mississippi must be enabled and public")
        selected(catalog.configured()[name], county)
        return json.loads(
            containers.import_county(
                value["release"]["image_id"], name, prod, config, county, report, review
            )
        )


# Dashboard ----------------------------------------------------------------------------


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


def wait_for(url, expected, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return dashboard.check(url, expected)
        except OSError:
            if time.monotonic() >= deadline:
                raise ValueError("Candidate dashboard did not become ready") from None
            time.sleep(1)


def start_dashboard(path, names, prod, config):
    """A private dashboard from the current release with ``names`` also made public."""
    value = catalog.read(path)
    for name in names:
        if value["sources"][name]["public"] or not value["sources"][name].get("publication_id"):
            raise ValueError(f"{name}: publish its first snapshot (prepare, promote) first")
    identity = uuid.uuid4().hex
    state = Path(config["CONTROL_STATE_DIR"])
    candidate = dashboard.start(
        value,
        names,
        prod,
        config,
        state / ("dashboard-" + identity),
        port=free_port(),
        release=value["release"],
    )
    candidate["id"] = identity
    wait_for(candidate["url"], dashboard.expected_races(catalog.published(value, names)))
    atomic_json(state / ("candidate-" + identity + ".json"), candidate)
    return candidate


def recovery_gate(value, names, report, now):
    """A restore check after the new states' publications, which it verified."""
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
    if not 0 <= (now - finished).total_seconds() <= 1800:
        raise ValueError("Restore check must be from the last 30 minutes")
    for name in names:
        entry = value["sources"][name]
        if finished < datetime.fromisoformat(entry["published_at"]):
            raise ValueError("Restore check must follow the new state's publication")
        if report.get("current_publications", {}).get(name) != entry["publication_id"]:
            raise ValueError("Restore check did not verify this state's published snapshot")
    if report.get("database_inventory", {}).get("serving.contest_results", {}).get("rows", 0) <= 0:
        raise ValueError("Restored published results are missing")


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
        dashboard.check(
            config.get("ORIGIN_URL", "http://127.0.0.1"), dashboard.expected_races(after)
        )
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


def activate(path, candidate, report, config):
    """Point the public site at a checked candidate dashboard; new states become public."""
    containers.limits(config)
    with catalog.locked(path, exclusive=True):
        value = catalog.read(path)
        if value["generation"] != candidate["catalog_generation"]:
            raise ValueError("Catalog changed; create and check a fresh dashboard candidate")
        if candidate["name"] != "election-dashboard-" + candidate["id"]:
            raise ValueError("Invalid candidate identity")
        names = candidate["sources"]
        if names:
            recovery_gate(value, names, report, datetime.now(timezone.utc))
        prospective = catalog.published(value, names)
        payload = dashboard.check(candidate["url"], dashboard.expected_races(prospective))
        for name in names:
            code = catalog.configured()[name].state
            if (
                str(payload["states"][code]["publication"]["publication_id"])
                != (prospective["sources"][name]["publication_id"])
            ):
                raise ValueError("The checked publication changed; validate and back up again")
        prospective["previous_dashboard"] = {
            **value["dashboard"],
            "caddyfile": Path(config["CADDYFILE"]).read_text(encoding="utf-8"),
            "source_flags": {
                name: {key: entry[key] for key in ("enabled", "public")}
                for name, entry in value["sources"].items()
            },
        }
        prospective["dashboard"] = candidate
        prospective["generation"] += 1
        switch_dashboard(path, value, prospective, candidate["name"], config)
    return {"outcome": "activated", "sources": names, "dashboard": candidate["name"]}


def rollback_dashboard(path, config):
    with catalog.locked(path, exclusive=True):
        before = catalog.read(path)
        value = json.loads(json.dumps(before))
        previous = value.get("previous_dashboard")
        if previous is None:
            raise ValueError("No previous dashboard is eligible for rollback")
        for name, flags in previous["source_flags"].items():
            if name in value["sources"]:
                value["sources"][name].update(flags)
        dashboard.check(previous["url"], dashboard.expected_races(value))
        value["dashboard"] = {
            k: v for k, v in previous.items() if k not in ("caddyfile", "source_flags")
        }
        value.pop("previous_dashboard")
        value["generation"] += 1
        switch_dashboard(path, before, value, previous["name"], config)
    return {"outcome": "rolled_back", "dashboard": previous["name"]}
