"""One command to take a source live: register through activation, each gate in code.

``deploy go-live --source xx_2026_general`` runs the steps of "adding a state in
production" (docs/deployment.md) in their fixed order and stops at the first one that
fails, so the order and its gates no longer live in notes:

1. ``register`` the source in the catalog (disabled, private);
2. ``configure`` it for manual imports, when it is one;
3. ``acquire`` its first snapshot: prepare it over HTTP, or import the operator's file;
4. ``promote`` the candidate to a (still private) publication;
5. ``backup``: an uploaded backup that holds the publication;
6. ``restore_check``: a passed isolated restore of that backup that verified *this*
   publication (``control.recovery_gate``, the same gate ``activate`` applies);
7. ``candidate``: a private dashboard that serves every race the catalog expects;
8. ``activate``: the public switch, undone if the public site fails its check.

Each step looks at the catalog, which is the truth, and skips what is already done, so
running the same command again resumes where an earlier run stopped. The journal
(``golive-<source>.json`` in the control state directory) records each step's outcome
and time for the operator. ``stop_before_activation`` leaves a checked candidate for
the operator to look at, then activates it by running the command again.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline.deployment import catalog, containers, control, manual
from election_pipeline.storage import atomic_json, utc_now

STEPS = (
    "register",
    "configure",
    "acquire",
    "promote",
    "backup",
    "restore_check",
    "candidate",
    "activate",
)
BUSY = ("active", "activating", "deactivating", "reloading")
# The backup's OnSuccess starts the restore check a moment after it; after this long
# without one running, the command runs the check itself.
START_GRACE_SECONDS = 90
POLL_SECONDS = 10


class Journal:
    """What a go-live has done, kept beside the catalog for the operator."""

    def __init__(self, directory, name):
        self.path = Path(directory) / f"golive-{name}.json"
        try:
            self.value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.value = {"schema_version": 1, "source_id": name, "steps": {}}
        self.value.update(outcome="running", started_at=self.value.get("started_at") or utc_now())

    def record(self, step, outcome, **detail):
        self.value["steps"][step] = {"outcome": outcome, "at": utc_now(), **detail}
        self.save()

    def finish(self, outcome, **detail):
        self.value.update(outcome=outcome, **detail)
        self.save()

    def save(self):
        self.value["updated_at"] = utc_now()
        atomic_json(self.path, self.value)


def verifies(entry, name, report, now):
    """Whether ``report`` is a restore check ``activate`` would accept for ``entry``."""
    try:
        control.recovery_gate({"sources": {name: entry}}, [name], report, now)
    except (ValueError, KeyError, TypeError):
        return False
    return True


def read_report(directory):
    try:
        return json.loads((Path(directory) / "latest-check-restore.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def backup_directory(config):
    return Path(config.get("BACKUP_DIR", "/srv/election-pipeline/backups"))


def backup(run):
    """An uploaded backup. The unit is a one-shot: starting it returns when the backup has
    finished, and fails if it failed."""
    run(["systemctl", "start", "election-backup.service"], timeout=3600)


def restore_check(path, name, config, *, run, sleep, monotonic, wait_seconds):
    """A passed restore check that verified the promoted publication, and its report.

    The backup's own follow-on check is waited for; if none starts, the check is run here
    once. The report is what ``activate`` takes as its gate.
    """
    release = Path(catalog.read(path)["release"]["release_path"])
    destination = backup_directory(config)
    deadline = monotonic() + wait_seconds
    idle_since, checked_here = None, False
    while True:
        entry = catalog.read(path)["sources"][name]
        report = read_report(destination)
        if verifies(entry, name, report, datetime.now(timezone.utc)):
            return report
        state = run(
            ["systemctl", "show", "--value", "-p", "ActiveState", "election-check-restore.service"]
        )
        now = monotonic()
        if state in BUSY:
            idle_since = None
        elif idle_since is None:
            idle_since = now
        elif now - idle_since >= START_GRACE_SECONDS:
            if checked_here:
                raise ValueError("The restore check did not verify this publication")
            checked_here = True
            run(
                [
                    sys.executable,
                    str(release / "scripts/production_recovery.py"),
                    "check-restore",
                    "--catalog-file",
                    str(path),
                ],
                timeout=3600,
            )
            idle_since = None
            continue
        if now >= deadline:
            raise ValueError("Timed out waiting for a restore check of this publication")
        sleep(POLL_SECONDS)


def go_live(
    path,
    name,
    prod,
    config,
    *,
    acquire=None,
    final=False,
    stop_before_activation=False,
    run=containers.command,
    sleep=time.sleep,
    monotonic=time.monotonic,
):
    """Take ``name`` from the catalog (or not yet in it) to public; resume where it stopped.

    ``acquire``: for a manual source, the operator's import (``manual.operate`` keywords:
    file, sha256, downloaded_at, generated_at, certified, attested). A source already
    configured for manual imports needs it only until its first candidate exists.
    ``final``: mark a manual source's issuer as posting nothing more (see ``configure``).
    """
    state = Path(config["CONTROL_STATE_DIR"])
    wait_seconds = int(config.get("GO_LIVE_RESTORE_WAIT_SECONDS", "3600"))
    with catalog.locked(state / f"golive-{name}", exclusive=True, nonblocking=True):
        journal = Journal(state, name)
        current = None

        def step(label, action, *, needed):
            nonlocal current
            current = label
            if not needed:
                journal.record(label, "skipped")
                return None
            result = action()
            journal.record(label, "done")
            return result

        try:
            entry = catalog.read(path)["sources"].get(name)
            if entry and entry["public"] and entry["enabled"]:
                journal.finish("done", note="already public")
                return {"outcome": "already_public", "source_id": name}
            step("register", lambda: control.register(path, name), needed=entry is None)
            entry = catalog.read(path)["sources"][name]
            step(
                "configure",
                lambda: manual.configure(path, name, final=final),
                needed=acquire is not None and entry["acquisition"] != "manual",
            )
            entry = catalog.read(path)["sources"][name]
            first = not entry.get("publication_id") and not entry.get("candidate_id")
            if first and entry["acquisition"] == "manual" and acquire is None:
                raise ValueError("A manual source needs its file: pass the import arguments")
            step(
                "acquire",
                lambda: (
                    manual.operate(path, name, prod, config, **acquire)
                    if entry["acquisition"] == "manual"
                    else control.prepare(path, name, prod, config)
                ),
                needed=first,
            )
            entry = catalog.read(path)["sources"][name]
            step(
                "promote",
                lambda: control.promote(path, name, prod, config),
                needed=not entry.get("publication_id"),
            )
            # A restore check that already verified this publication (an earlier run got
            # this far) is not repeated.
            entry = catalog.read(path)["sources"][name]
            report = read_report(backup_directory(config))
            held = verifies(entry, name, report, datetime.now(timezone.utc))
            step("backup", lambda: backup(run), needed=not held)
            if not held:
                report = step(
                    "restore_check",
                    lambda: restore_check(
                        path,
                        name,
                        config,
                        run=run,
                        sleep=sleep,
                        monotonic=monotonic,
                        wait_seconds=wait_seconds,
                    ),
                    needed=True,
                )
            else:
                journal.record("restore_check", "skipped")
            candidate = step(
                "candidate",
                lambda: control.start_dashboard(path, [name], prod, config),
                needed=True,
            )
            if stop_before_activation:
                journal.finish("stopped", candidate=candidate["id"])
                return {
                    "outcome": "stopped_before_activation",
                    "source_id": name,
                    "candidate": candidate["id"],
                    "url": candidate["url"],
                }
            result = step(
                "activate",
                lambda: control.activate(path, candidate, report, config),
                needed=True,
            )
        except Exception as error:
            message = control.redacted(str(error), prod)
            journal.record(current or "register", "failed", error=message[-2000:])
            journal.finish("failed")
            raise ValueError(
                f"Go-live of {name} stopped at {current}: {message}. "
                "Fix it and run the same command again: finished steps are skipped."
            ) from error
        journal.finish("done")
        return {
            "outcome": "live",
            "source_id": name,
            "activation": result,
            "journal": str(journal.path),
        }
