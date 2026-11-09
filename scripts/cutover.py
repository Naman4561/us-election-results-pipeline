"""One-time move of a production host from per-source pins to one release.

Run from the staged release's own venv, as root, after an uploaded backup. With R as
/opt/election-releases/<revision>:

    sudo R/.venv/bin/python R/scripts/cutover.py --release R --image election-pipeline:<revision>
    sudo R/.venv/bin/python R/scripts/cutover.py ... --apply

The first prints the plan; ``--apply`` performs it.

Steps, in order: stop both collectors and wait for running jobs; apply migration 024;
run every enabled HTTP source once, check-only, in the new image (if any fails, the
collectors restart and nothing else changes); copy every per-state row into the shared
tables (each schema verified, see election_pipeline.consolidate); write the version 2
catalog and deployment settings;
point ``current`` at the release; install the units (one worker instead of the
baseline and expansion collectors); move the public site to a dashboard from the release.
To undo, restore the backup taken before (scripts/production_recovery.py).
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.consolidate import consolidate
from election_pipeline.database import connect
from election_pipeline.deployment import catalog, containers, control
from election_pipeline.migrations import migrate
from election_pipeline.storage import replace_private

DEPLOYMENT_ENV = Path("/etc/election-pipeline/deployment.env")
UNITS = Path("/etc/systemd/system")
RETIRED_UNITS = (
    "election-pipeline.timer",
    "election-pipeline.service",
    "election-expansion.timer",
    "election-expansion.service",
)
INSTALLED_UNITS = (
    "election-worker.service",
    "election-worker.timer",
    "election-backup.service",
    "election-monitor.service",
)
# Settings kept from the pinned deployment, and the renamed resource budgets.
KEPT = (
    "PRODUCTION_ENV",
    "CATALOG_FILE",
    "CONTROL_STATE_DIR",
    "DOCKER_NETWORK",
    "CADDYFILE",
    "CADDY_CONTAINER",
    "ORIGIN_URL",
    "JOB_TIMEOUT",
    "CYCLE_SECONDS",
)
# Renamed budgets, with the pinned deployment's defaults for a setting it left out.
RENAMED = {"EXPANSION_CPUS": ("JOB_CPUS", "0.5"), "EXPANSION_MEMORY": ("JOB_MEMORY", "768m"),
           "EXPANSION_PARALLEL": ("JOB_PARALLEL", "1")}  # fmt: skip
SOURCE_KEYS = (
    "enabled",
    "public",
    "status",
    "publication_id",
    "published_at",
    "manual_max_age_seconds",
    "manual_final",
    "manual_snapshot",
)


def convert_catalog(old, release):
    """The version 2 catalog: the release, the dashboard, and each current source's flags.

    Sources the release no longer configures (the retired originals) leave the catalog;
    their archives and publications stay where they are.
    """
    if old.get("version") != 1:
        raise ValueError("Expected the version 1 (per-source pin) catalog")
    configured = catalog.configured()
    sources = {}
    for name, entry in old["sources"].items():
        if name not in configured:
            continue
        sources[name] = {k: entry[k] for k in SOURCE_KEYS if k in entry}
        # Results sources imported by hand stay manual; every other source is HTTP.
        manual = entry["worker"] == "manual" and configured[name].dataset == "election_results"
        sources[name]["acquisition"] = "manual" if manual else "http"
    value = {
        "version": 2,
        "generation": old["generation"] + 1,
        "release": release,
        "previous_release": None,
        # Replaced by the release's own dashboard at the last step.
        "dashboard": {k: old["dashboard"][k] for k in ("name", "url", "states")},
        "sources": sources,
    }
    return catalog.validate(value)


def convert_settings(old, current_link):
    lines = ["# Written by scripts/cutover.py. Paths and resource budgets, not passwords."]
    for key in KEPT:
        lines.append(f"{key}={old[key]}")
    for before, (after, default) in RENAMED.items():
        lines.append(f"{after}={old.get(before, default)}")
    lines.append(f"CURRENT_RELEASE={current_link}")
    return "\n".join(lines) + "\n"


def recent_backup(prod, now):
    latest = Path(prod.get("BACKUP_DIR", "/srv/election-pipeline/backups")) / "latest-backup.json"
    record = json.loads(latest.read_text(encoding="utf-8"))
    age = (now - datetime.fromisoformat(record["finished_at"])).total_seconds()
    if record.get("outcome") != "uploaded" or not 0 <= age <= 3600:
        raise ValueError("Take an uploaded backup (election-backup.service) less than an hour ago")
    return record["backup_id"]


def systemctl(*args):
    return containers.command(["systemctl", *args], timeout=120)


def start_collectors():
    systemctl(
        "start", "election-pipeline.timer", "election-expansion.timer", "election-monitor.timer"
    )


def stop_collectors():
    # The monitor would read the new catalog with old code mid-cutover; it restarts at the end.
    systemctl(
        "stop", "election-pipeline.timer", "election-expansion.timer", "election-monitor.timer"
    )
    deadline = time.monotonic() + 600
    for service in ("election-pipeline.service", "election-expansion.service"):
        while systemctl("show", "--value", "-p", "ActiveState", service) in (
            "active",
            "activating",
            "deactivating",
        ):
            if time.monotonic() > deadline:
                raise ValueError(f"{service} is still running; retry when it finishes")
            time.sleep(5)


def install_units(release):
    for unit in RETIRED_UNITS:
        subprocess.run(["systemctl", "disable", unit], capture_output=True, check=False)
        (UNITS / unit).unlink(missing_ok=True)
    for unit in INSTALLED_UNITS:
        shutil.copyfile(Path(release) / "deploy/systemd" / unit, UNITS / unit)
        # The pinned deployment pointed these at its control checkout with drop-ins.
        shutil.rmtree(UNITS / (unit + ".d"), ignore_errors=True)
    systemctl("daemon-reload")
    systemctl("enable", "--now", "election-worker.timer")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--apply", action="store_true", help="Perform the steps (default: plan)")
    args = parser.parse_args()
    old_settings = dict(dotenv_values(DEPLOYMENT_ENV, interpolate=False))
    prod = dict(dotenv_values(old_settings["PRODUCTION_ENV"], interpolate=False))
    path = Path(old_settings["CATALOG_FILE"])
    current = Path(args.release).resolve().parent / "current"
    release = control.describe(args.release, args.image)
    old = json.loads(path.read_text(encoding="utf-8"))
    value = convert_catalog(old, release)
    settings = convert_settings(old_settings, current)
    backup = recent_backup(prod, datetime.now(timezone.utc))
    plan = {
        "backup_before_cutover": backup,
        "release": release["revision"],
        "sources": {
            n: {k: e[k] for k in ("acquisition", "enabled", "public")}
            for n, e in value["sources"].items()
        },  # fmt: skip
        "sources_to_validate": control.collected(value),
        "retired_from_catalog": sorted(set(old["sources"]) - set(value["sources"])),
        "units_removed": RETIRED_UNITS,
        "units_installed": INSTALLED_UNITS,
    }
    if not args.apply:
        print(json.dumps({"outcome": "planned", **plan}, indent=2))
        return 0

    stop_collectors()
    with connect(control.db_settings(prod, writer=True)) as conn:
        # Additive: the pinned images keep working if the cutover stops below.
        plan["migrations_applied"] = migrate(conn, root=release["release_path"])
    budgets = dotenv_values(stream=io.StringIO(settings), interpolate=False)
    failed = control.check_sources(release["image_id"], value, prod, budgets)
    if failed:
        start_collectors()
        raise ValueError(
            control.redacted("The release did not validate: " + json.dumps(failed), prod)
        )
    with connect(control.db_settings(prod, writer=True)) as conn:
        plan["consolidation"] = consolidate(conn)
    with catalog.locked(path, exclusive=True):
        catalog.write(path, value)
    replace_private(DEPLOYMENT_ENV, settings)
    config = dict(dotenv_values(DEPLOYMENT_ENV, interpolate=False))
    control.point_current(config, release["release_path"])
    install_units(release["release_path"])
    candidate = control.start_dashboard(path, [], prod, config)
    plan["dashboard"] = control.activate(path, candidate, {}, config)["dashboard"]
    # The pinned deployment's dashboards read the per-state tables, which no longer change.
    for name in re.findall(
        r"election-expansion-dashboard-[0-9a-f]{32}",
        containers.command(["docker", "ps", "-a", "--format", "{{.Names}}"]),
    ):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    value = catalog.read(path)
    value.pop("previous_dashboard", None)
    catalog.write(path, value)
    systemctl("start", "election-monitor.timer")
    print(json.dumps({"outcome": "cut_over", **plan}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
