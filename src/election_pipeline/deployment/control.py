"""Host-side control of the one release that runs every state.

A release is staged (scripts/stage_release.sh: checkout, image, tests) and then made
current with ``release``: migrations apply, every enabled HTTP source must validate a
publication candidate under the new image, and only then do the catalog and the
``current`` symlink (which the systemd units run from) move. The public dashboard moves
separately, after a candidate dashboard from the release passes its checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline.database import connect
from election_pipeline.deployment import catalog, containers, dashboard
from election_pipeline.migrations import migrate
from election_pipeline.publication import attestation
from election_pipeline.storage import atomic_json, utc_now
from election_pipeline.workflow import NO_SNAPSHOT

# A locked source is held by another run or a backup; it is not a failure.
GOOD = {"succeeded", "already_current", "not_due", "awaiting_publication", "locked", "paused"}
# A release check of a locked source is retried this often until CHECK_LOCK_WAIT_SECONDS.
LOCK_RETRY_SECONDS = 15
# After a release: the most recent other releases kept beside the current and previous.
KEEP_RECENT_RELEASES = 2
# The Docker build cache kept for the next staging; older layers beyond it are pruned.
BUILD_CACHE_BYTES = 4 * 1024**3
REVISION = re.compile(r"[0-9a-f]{40}")
# What a source's release check can run: the image's build inputs, dbt models,
# migrations, the job scripts and the package (see check_keys for which modules).
CHECK_INPUTS = (
    "Dockerfile", "constraints.txt", "pyproject.toml", "dbt/dbt_project.yml",
    "dbt/macros", "dbt/models", "dbt/tests", "migrations", "src/election_pipeline",
    "scripts/run_source.py", "scripts/run_dbt.py", "scripts/publish.py",
)  # fmt: skip
IMPORT = re.compile(
    r"^\s*(?:from\s+(election_pipeline[\w.]*)\s+import\s+(\([^)]*\)|[^\n]+)"
    r"|import\s+(election_pipeline[\w.]*))",
    re.M,
)
CHECK_LEDGER = "release-checks.json"
# A source that is not due and whose last run already published its snapshot under the
# current image is skipped, not started in a container to say so; it still runs at
# least this often, which keeps its run report fresh for monitoring.
IDLE_RUN_SECONDS = 1800
IDLE_LEDGER = "worker-idle.json"
# How old a restore check may be when it admits a new state. It must have verified that
# state's exact published snapshot, after it was published, so its age only bounds how
# long ago the backup path itself last worked. election-check-restore.service runs one
# after the first backup that holds a newly promoted state.
RESTORE_CHECK_SECONDS = 24 * 3600


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


def run_one(image, name, status, prod, config, *, mode=None, extra=()):
    """Run one source in the release image; ``mode`` is prepare-only or check-only."""
    args = ["scripts/run_source.py", "--source", name, "--status", status]
    if mode:
        args.append("--" + mode)
    args += extra
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


def idle(name, entry, image, prod, ledger, settings, sources, now):
    """Whether running ``name`` now would only find its publication current again: it is
    not due for collection, and its last run, recent, published the snapshot it holds
    under this image and status."""
    from election_pipeline.pipeline import next_collection

    data, source = Path(prod["DATA_DIR"]), sources[name]
    try:
        state = json.loads((data / "state" / f"{name}.json").read_text(encoding="utf-8"))
        ran = (data / "runs" / name / "latest.json").stat().st_mtime
    except (OSError, ValueError):
        return False
    waiting = [state.get(k) for k in ("retry_not_before", "failure_backoff_until")]
    if (
        ledger.get(name)
        != {"image": image, "status": entry["status"], "raw_sha256": state.get("sha256")}
        or now.timestamp() - ran >= IDLE_RUN_SECONDS
        or not (source.min_interval_seconds or source.collect_hours_utc)
        or state.get("source_signature") != source.signature(settings)
        or state.get("url") != source.url
        or state.get("consecutive_failures")
        or any(waiting)
        or not state.get("last_checked_at")
    ):
        return False
    return now < next_collection(source, datetime.fromisoformat(state["last_checked_at"]), now)


def run_enabled(path, prod, config):
    """One bounded cycle over every enabled HTTP source, least recently run first."""
    from election_pipeline.settings import load_config

    value = catalog.read(path)
    containers.limits(config)
    deadline = time.monotonic() + int(config.get("CYCLE_SECONDS", "270"))
    settings, sources = load_config(catalog.CONFIG)
    ledger_path = Path(config["CONTROL_STATE_DIR"]) / IDLE_LEDGER
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))["sources"]
    except (OSError, ValueError, KeyError):
        ledger = {}

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
            if entry.get("paused"):
                return {"source_id": name, "outcome": "paused"}
            image = current["release"]["image_id"]
            now = datetime.now(timezone.utc)
            try:
                skip = idle(name, entry, image, prod, ledger, settings, sources, now)
            except Exception:
                skip = False  # unsure: run it
            if skip:
                return {"source_id": name, "outcome": "not_due", "skipped": "idle"}
            remaining = max(1, int(deadline - time.monotonic()))
            budget = {
                **config,
                "JOB_TIMEOUT": str(min(int(config.get("JOB_TIMEOUT", "240")), remaining)),
            }
            result = run_one(image, name, entry["status"], prod, budget)
            if result.get("outcome") in ("already_current", "succeeded") and result.get(
                "raw_sha256"
            ):
                result["_idle"] = {
                    "image": image,
                    "status": entry["status"],
                    "raw_sha256": result["raw_sha256"],
                }
        except Exception as error:
            result = {"source_id": name, "outcome": "failed", "error": str(error)}
        marker = result.pop("_idle", None)
        encoded = redacted(json.dumps(result), prod)
        print(encoded, flush=True)
        return {**json.loads(encoded), "_idle": (name, marker)}

    # Each job has its own container limits, so N parallel jobs use N times the budget.
    with ThreadPoolExecutor(max_workers=parallel_jobs(config)) as pool:
        results = [
            r for r in pool.map(job, sorted(collected(value), key=last_run)) if r is not None
        ]
    for r in results:
        name, marker = r.pop("_idle", (r.get("source_id"), None))
        if marker:
            ledger[name] = marker
        elif r["outcome"] not in ("deferred", "paused") and r.get("skipped") != "idle":
            # Only a run that did not publish its snapshot clears the record.
            ledger.pop(name, None)
    atomic_json(ledger_path, {"schema_version": 1, "sources": ledger})
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


def images_in_use():
    """The image IDs every container, running or stopped, was created from."""
    identities = containers.command(["docker", "ps", "-aq", "--no-trunc"]).split()
    if not identities:
        return set()
    return set(
        containers.command(["docker", "inspect", "--format", "{{.Image}}", *identities]).split()
    )


def protected(prod, config):
    """The data and backup folders, which pruning must never reach."""
    return [
        Path(p).resolve()
        for p in (prod.get("DATA_DIR"), config.get("BACKUP_DIR", "/srv/election-pipeline/backups"))
        if p
    ]


def prune_releases(config, value, now=None, keep_out=()):
    """Remove the release checkouts, images and host environments no release needs.

    Kept: the current and previous releases, the ``KEEP_RECENT_RELEASES`` newest others
    (a release staged but not yet made current among them), and every release whose image
    a container was created from, as the active and previous dashboards are. A host
    environment is kept while a kept release's venv names it, or for an hour after it
    changed (a staging may be building one). The build cache is trimmed to
    ``BUILD_CACHE_BYTES``. Returns what was removed.

    Only folders named like a release (40 hex digits) or a host environment (16) are
    removed, and nothing is if either parent folder overlaps ``keep_out`` (the data and
    backup folders): election data is never pruned here.
    """
    now = now or time.time()
    link = Path(config["CURRENT_RELEASE"])
    root = link.parent
    environments = Path(config.get("HOST_ENVIRONMENTS", "/opt/election-venvs"))
    for folder in (root.resolve(), environments.resolve()):
        for guarded in keep_out:
            if folder.is_relative_to(guarded) or guarded.is_relative_to(folder):
                raise ValueError(f"Refusing to prune {folder}: it overlaps {guarded}")
    used = images_in_use()
    tagged = dict(
        line.split()
        for line in containers.command(
            ["docker", "images", "--no-trunc", "--format", "{{.Tag}} {{.ID}}", "election-pipeline"]
        ).splitlines()
        if line.strip()
    )
    keep = {e["revision"] for e in (value["release"], value.get("previous_release")) if e}
    keep |= {revision for revision, identity in tagged.items() if identity in used}
    others = sorted(
        (
            p
            for p in root.iterdir()
            if REVISION.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
            if p.name not in keep
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    keep |= {p.name for p in others[:KEEP_RECENT_RELEASES]}
    if link.resolve().name not in keep:
        raise ValueError("The current release is not among those kept; nothing pruned")
    removed = {"releases": [], "images": [], "environments": []}
    for path in others[KEEP_RECENT_RELEASES:]:
        shutil.rmtree(path)
        removed["releases"].append(path.name)
    for revision, identity in sorted(tagged.items()):
        if REVISION.fullmatch(revision) and revision not in keep and identity not in used:
            try:
                containers.command(["docker", "rmi", "election-pipeline:" + revision])
            except RuntimeError:
                continue  # in use after all; the next release tries again
            removed["images"].append(revision)
    if environments.is_dir():
        named = set()
        for revision in keep:
            for entry in (root / revision / ".venv").glob("lib/python*/site-packages/*.pth"):
                named.update(
                    re.findall(
                        re.escape(str(environments)) + r"/([0-9a-f]{16})/",
                        entry.read_text(encoding="utf-8"),
                    )
                )
        for path in sorted(environments.iterdir()):
            if (
                re.fullmatch(r"[0-9a-f]{16}", path.name)
                and path.name not in named
                and now - path.stat().st_mtime > 3600
            ):
                shutil.rmtree(path)
                removed["environments"].append(path.name)
    containers.command(["docker", "image", "prune", "-f"], timeout=300)
    containers.command(
        ["docker", "builder", "prune", "-f", "--max-used-space", str(BUILD_CACHE_BYTES)],
        timeout=600,
    )
    return removed


def point_current(config, release_path):
    """Atomically repoint the ``current`` symlink the systemd units run from."""
    link = Path(config["CURRENT_RELEASE"])
    temporary = link.with_name(".current-" + uuid.uuid4().hex)
    os.symlink(release_path, temporary)
    os.replace(temporary, link)


def check_modules(root, code):
    """The package modules a check of state ``code`` can run: everything the two job
    scripts import, its own parser and collector (imported by name), and what they import.
    A module only another state's parser or a host-side tool reaches is left out."""
    from election_pipeline.states import STATES

    state = STATES[code]
    package = root / "src"

    def path(name):
        for found in (
            package / (name.replace(".", "/") + ".py"),
            package / name.replace(".", "/") / "__init__.py",
        ):
            if found.is_file():
                return found
        return None

    pending = [root / "scripts/run_source.py", root / "scripts/run_dbt.py"]
    pending.append(path(f"election_pipeline.parsers.{state.parser_module or state.slug}"))
    if state.collection_backend:
        pending.append(path(f"election_pipeline.collectors.{state.collection_backend}"))
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or current in seen:
            continue
        seen.add(current)
        for match in IMPORT.finditer(current.read_text(encoding="utf-8")):
            module, names, plain = match.groups()
            module = module or plain
            parts = module.split(".")
            # A module's packages run their __init__ first.
            pending += [path(".".join(parts[:n])) for n in range(1, len(parts) + 1)]
            for name in re.findall(r"\w+", names or ""):
                pending.append(path(f"{module}.{name}"))
    return {item for item in seen if package in item.parents}


def check_keys(release_path, value, prod, names):
    """{source: the code its release check depends on}; a source with an unchanged key and
    a recorded pass need not be checked again (see ``release`` for its snapshot). The key
    covers the release files its check runs (build inputs, dbt, migrations, job scripts,
    the modules ``check_modules`` finds, reviewed data other than other states'), and the
    source's configuration and status. A source without a snapshot gets no key."""
    from election_pipeline.settings import load_config
    from election_pipeline.states import STATES, own_registry

    root = Path(release_path)
    settings, sources = load_config(root / "config/sources.toml")
    resources = root / "src/election_pipeline/resources"
    registry = root / "src/election_pipeline/states.py"
    # The dashboard's own files (styles, maps, its script) are served, never read by a check.
    static = root / "src/election_pipeline/dashboard/static"
    # A state's own data (``<slug>_*.json``) is read by its parser alone. Rosters are not
    # theirs alone: every parser imports all of them (parsers.scope_2026).
    owners = {}
    for code, state in STATES.items():
        for item in resources.glob(f"{state.slug}_*.json"):
            owners[item] = code
    files = {}
    for entry in CHECK_INPUTS:
        found = root / entry
        if not found.exists():
            raise ValueError(f"Release lacks {entry}")
        for item in sorted(found.rglob("*")) if found.is_dir() else [found]:
            if item.is_file() and "__pycache__" not in item.parts:
                digest = hashlib.sha256(item.read_bytes()).hexdigest()
                files[item] = f"{item.relative_to(root).as_posix()}:{digest}"
    keys, modules = {}, {}
    for name in names:
        source = sources[name]
        state = json.loads(
            (Path(prod["DATA_DIR"]) / "state" / f"{name}.json").read_text(encoding="utf-8")
        )
        if not state.get("sha256") or state.get("url") != source.url:
            continue
        if source.state not in modules:
            modules[source.state] = check_modules(root, source.state)
        used = [
            text
            for item, text in files.items()
            if item != registry
            and static not in item.parents
            and (
                item.suffix != ".py" or item in modules[source.state]
                if resources not in item.parents
                else owners.get(item, source.state) == source.state
            )
        ]
        # The registry counts only as far as it concerns this state.
        used.append(own_registry(registry, source.state))
        digest = hashlib.sha256()
        for part in (source.signature(settings), value["sources"][name]["status"], *used):
            digest.update(str(part).encode() + b"\0")
        keys[name] = digest.hexdigest()
    return keys


def snapshots(prod, names):
    """{source: the snapshot its release check would read}."""
    found = {}
    for name in names:
        try:
            state = json.loads(
                (Path(prod["DATA_DIR"]) / "state" / f"{name}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if state.get("sha256"):
            found[name] = state["sha256"]
    return found


def published(value, config, current):
    """Sources whose current snapshot the worker last published under the live release's
    image and status. For one whose check key the live release shares, checking that
    snapshot again would only repeat that run."""
    try:
        ledger = json.loads(
            (Path(config["CONTROL_STATE_DIR"]) / IDLE_LEDGER).read_text(encoding="utf-8")
        )["sources"]
    except (OSError, ValueError, KeyError):
        return set()
    image = value["release"]["image_id"]
    return {
        name
        for name, snapshot in current.items()
        if ledger.get(name)
        == {"image": image, "status": value["sources"][name]["status"], "raw_sha256": snapshot}
    }


def check_sources(image, value, prod, config, moved=(), names=None):
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
        # A check publishes nothing. An official source's snapshot may be certified by an
        # operator's attestation rather than its own feed, so it is checked as unofficial.
        status = value["sources"][name]["status"]
        # A source the worker is collecting right now is locked; wait for it to finish
        # rather than fail the release.
        deadline = time.monotonic() + int(config.get("CHECK_LOCK_WAIT_SECONDS", "600"))
        while True:
            try:
                result = run_one(
                    image,
                    name,
                    "unofficial" if status == "official" else status,
                    prod,
                    config,
                    mode="check-only",
                )
            except Exception as error:
                result = {"source_id": name, "outcome": "failed", "error": str(error)}
            if result["outcome"] != "locked" or time.monotonic() >= deadline:
                return name, result
            time.sleep(LOCK_RETRY_SECONDS)

    with ThreadPoolExecutor(max_workers=parallel_jobs(config)) as pool:
        checks = dict(pool.map(check, collected(value) if names is None else names))
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


def release(path, release_path, image, prod, config, moved=(), check_all=False):
    """Make a staged release current once it validates every enabled HTTP source.

    A source whose check key (``check_keys``) passed before, under this or an earlier
    release, is not checked again unless ``check_all``, provided its snapshot is the one
    that passed or one the worker has since published under a live release with the same
    key.
    """
    staged = describe(release_path, image)
    value = catalog.read(path)
    if staged["revision"] == value["release"]["revision"]:
        return {"outcome": "already_current", "revision": staged["revision"]}
    with connect(db_settings(prod, writer=True)) as conn:
        applied = migrate(conn, root=staged["release_path"])
    names = collected(value)
    ledger = Path(config["CONTROL_STATE_DIR"]) / CHECK_LEDGER
    try:
        keys = check_keys(staged["release_path"], value, prod, set(names) - set(moved))
        current = snapshots(prod, keys)
        proven = published(value, config, current)
        recorded = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
        if recorded.get("schema_version") != 2:
            recorded = {}  # a version 1 key covered its snapshot: check once more
        passed, live = recorded.get("passed", {}), recorded.get("released", {})
    except Exception:
        # Without keys every source is checked, as before.
        keys, current, proven, passed, live = {}, {}, set(), {}, {}

    def reusable(name):
        recorded = passed.get(name, {})
        return recorded.get("key") == keys[name] and (
            recorded.get("snapshot") == current.get(name)
            or (live.get(name) == keys[name] and name in proven)
        )

    reused = [] if check_all else sorted(n for n in keys if reusable(n))
    if reused and len(reused) == len(names):
        # One source is always checked, a different one each release: it proves the new
        # image runs end to end, which no key can show (a manual-only state's registry
        # entry, say, is in no collected source's key).
        reused.remove(
            min(reused, key=lambda n: hashlib.sha256((staged["revision"] + n).encode()).digest())
        )
    checked = [n for n in names if n not in reused]
    failed = check_sources(staged["image_id"], value, prod, config, moved, checked)
    # A pass holds for its key whether or not this release goes ahead.
    passed = {
        **{n: k for n, k in passed.items() if n not in failed},
        **{
            n: {"key": keys[n], "snapshot": current.get(n)}
            for n in checked
            if n in keys and n not in failed
        },
    }
    atomic_json(ledger, {"schema_version": 2, "passed": passed, "released": live})
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
    # The worker now runs this release: its keys say which later releases share its code.
    atomic_json(ledger, {"schema_version": 2, "passed": passed, "released": dict(keys)})
    result = {
        "outcome": "released",
        "revision": staged["revision"],
        "previous_revision": value["release"]["revision"],
        "migrations_applied": applied,
        "sources_validated": checked,
        "sources_reused": reused,
        "note": "Workers use the release from their next cycle; move the dashboard next",
    }
    # The release is made; old artifacts that fail to go are reported, not a failure.
    try:
        result["pruned"] = prune_releases(
            config, catalog.read(path), keep_out=protected(prod, config)
        )
    except Exception as error:
        result["prune_error"] = str(error)
    return result


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


def register(path, name):
    """Add a state's results source, new in this release's configuration, to the catalog.

    It starts as every new source does: disabled, private, unofficial and collected over
    HTTP, so nothing is fetched or shown until it is prepared, promoted and activated.
    """
    from election_pipeline.states import STATES

    source = catalog.configured().get(name)
    if source is None or source.dataset != "election_results":
        raise ValueError("Register a results source configured in this release")
    if STATES.get(source.state) is None or STATES[source.state].source_id != name:
        raise ValueError(f"{name} is not its state's results source")

    def change(value):
        if name in value["sources"]:
            raise ValueError(f"{name} is already in the catalog")
        value["sources"][name] = {
            "acquisition": "http",
            "enabled": False,
            "public": False,
            "status": "unofficial",
        }

    catalog.update(path, change)
    return {"outcome": "registered", "source_id": name, "enabled": False, "public": False}


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


def certify(path, name, prod, config, *, approve=None, certified=None, attested=None):
    """Publish a public source's official results.

    Run after checking the totals against the state's certified results. With neither
    option, the state's own feed must mark its newest snapshot official. The two
    operator-attested modes are for states whose feed has no official marker:

    - ``approve``: the raw SHA-256 of the snapshot published now, which the operator
      found equal to the certified results; it is republished as official.
    - ``certified``: the state's separately published certified file, as a dict of
      ``file`` (under the data inbox), ``sha256``, ``downloaded_at`` and ``name``
      (``certified-<format>``, see ``parsers.certified``); it is imported into the
      source's archive, parsed and published as official.

    Both need ``attested``: evidence_url, certified_on, attested_by and attested_at.

    Collection is paused first, so the worker cannot publish an unofficial snapshot over
    the official one; the source then stays paused with status "official". A refused
    certification restores the pause as it was.
    """
    if approve is not None and certified is not None:
        raise ValueError("Approve a snapshot or import a certified file, not both")
    if (approve is None and certified is None) != (attested is None):
        raise ValueError("Approving a snapshot needs an attestation, and only then")
    value = catalog.read(path)
    entry = value["sources"][name]
    if not entry["public"] or entry["acquisition"] != "http":
        raise ValueError("Certify a public HTTP source")
    if entry["status"] == "official":
        return {"outcome": "already_official", "source_id": name}
    image = value["release"]["image_id"]
    if approve is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", approve):
            raise ValueError("Approve a snapshot by its full raw SHA-256")
        attested = attestation({**attested, "mode": "approved_snapshot"})
        extra = (
            "--replay-sha256",
            approve,
            "--publish-replay",
            "--attestation",
            json.dumps(attested, sort_keys=True),
        )

        def publish():
            return run_one(image, name, "official", prod, config, extra=extra)

    elif certified is not None:
        attested = attestation({**attested, "mode": "certified_file"})
        inbox = (Path(prod["DATA_DIR"]) / "inbox").resolve()
        relative = Path(certified["file"]).resolve(strict=True).relative_to(inbox)
        job = [
            "scripts/run_manual_source.py",
            "--source",
            name,
            "--file",
            str(Path("/app/data/inbox") / relative),
            "--sha256",
            certified["sha256"],
            "--downloaded-at",
            certified["downloaded_at"],
            "--certified",
            certified["name"],
            "--status",
            "official",
            "--publish",
            "--attestation",
            json.dumps(attested, sort_keys=True),
        ]

        def publish():
            output = containers.run_job(
                image, prod, config, job, timeout=int(config.get("JOB_TIMEOUT", "240"))
            )
            result = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            if result[-1].get("source_id") != name:
                raise ValueError("Worker returned the wrong source identity")
            return result[-1]

    else:

        def publish():
            return run_one(image, name, "official", prod, config)

    was_paused = entry.get("paused", False)
    pause(path, name, True)
    try:
        result = publish()
    except Exception:
        pause(path, name, was_paused)
        raise
    if result["outcome"] not in ("succeeded", "already_current"):
        pause(path, name, was_paused)
        raise ValueError(f"Official publication failed: {result.get('error', result['outcome'])}")

    def change(current):
        current["sources"][name]["status"] = "official"

    catalog.update(path, change)
    return {
        "outcome": "certified",
        "source_id": name,
        "raw_sha256": result.get("raw_sha256"),
        "attested": attested is not None,
    }


def pause(path, name, paused):
    """Stop or restart collecting a source; its last publication stays served either way."""

    def change(value):
        entry = value["sources"][name]
        if paused:
            entry["paused"] = True
        else:
            entry.pop("paused", None)

    catalog.update(path, change)
    return {"outcome": "paused" if paused else "resumed", "source_id": name}


def mark_unofficial(path, name):
    """Label a source that was brought up as ``test`` as the state's unofficial results.

    Only ``test`` moves, and only to ``unofficial``: an official source stays official.
    The worker's next run publishes its current snapshot again under the new status.
    """

    def change(value):
        entry = value["sources"][name]
        if entry["status"] != "test":
            raise ValueError(f"{name} is {entry['status']}, not test")
        entry["status"] = "unofficial"

    catalog.update(path, change)
    return {"outcome": "marked_unofficial", "source_id": name}


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


def county_final(path, county):
    """Record that a Mississippi county's reviewed manual snapshot is its last before the
    state certifies (its official report is posted): the monitor stops reminding that it
    is over six hours old. A later reviewed import is still possible."""
    from election_pipeline.county_import import selected
    from election_pipeline.states import STATES

    name = STATES["MS"].source_id
    selected(catalog.configured()[name], county)

    def change(value):
        entry = value["sources"][name]
        entry["manual_final_counties"] = sorted({*entry.get("manual_final_counties", []), county})

    catalog.update(path, change)
    return {"outcome": "county_final", "source_id": name, "county": county}


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


def wait_for(url, expected, seconds=180):
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
    # A new candidate supersedes older ones: at most the active, previous and this one run.
    dashboard.prune(value)
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
    # A new dashboard reads every state at startup; on a host busy with collection that
    # can take minutes.
    wait_for(
        candidate["url"],
        dashboard.expected_races(catalog.published(value, names)),
        int(config.get("DASHBOARD_READY_SECONDS", "180")),
    )
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
    if not 0 <= (now - finished).total_seconds() <= RESTORE_CHECK_SECONDS:
        raise ValueError("Restore check must be from the last 24 hours")
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
    result = {"outcome": "activated", "sources": names, "dashboard": candidate["name"]}
    # The switch is done; a dashboard that fails to stop is reported, not undone.
    try:
        result["discarded"] = dashboard.prune(prospective)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result["discard_error"] = str(error)
    return result


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
