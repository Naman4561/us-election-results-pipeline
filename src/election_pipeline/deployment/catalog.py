"""A credential-free registry shared by backups, monitoring and the dashboard."""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from election_pipeline.settings import CollectorSettings, Source

IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")


def source_from_json(value):
    raw = dict(value)
    for key in ("expected_members", "expected_columns", "contests", "supplemental_members"):
        raw[key] = tuple(raw.get(key, ()))
    raw["files"] = tuple(tuple(pair) for pair in raw.get("files", ()))
    raw["collector_overrides"] = tuple(tuple(pair) for pair in raw.get("collector_overrides", ()))
    return Source(**raw)


def expands_coverage(old, new):
    """Explicit county replacements preserve prior feeds while expanding admission."""
    if set(old["races"]) < set(new["races"]):
        return True
    from election_pipeline.states import STATES

    previous, candidate = source_from_json(old["source"]), source_from_json(new["source"])
    adapter = STATES.get(candidate.state)
    return bool(
        adapter
        and adapter.collection_backend == "county_files"
        and old["races"] == new["races"]
        and previous.state == candidate.state
        and previous.election_date == candidate.election_date
        and previous.dataset == candidate.dataset == "election_results"
        and previous.format == candidate.format == "bundle"
        and set(previous.files) <= set(candidate.files)
        and set(previous.expected_members) <= set(candidate.expected_members)
        and candidate.supplemental_members
    )


def validate(value):
    if value.get("version") != 1 or type(value.get("generation")) is not int:
        raise ValueError("Unsupported deployment catalog")
    baseline = value["baseline"]
    if not IMAGE.fullmatch(baseline["image_id"]) or not REVISION.fullmatch(baseline["revision"]):
        raise ValueError("Baseline requires an immutable image ID and Git revision")
    sources, owners = value["sources"], {}
    for name, entry in sources.items():
        source = source_from_json(entry["source"])
        CollectorSettings(**entry["settings"])
        if name != source.source_id or entry["worker"] not in ("baseline", "expansion", "manual"):
            raise ValueError("Invalid source identity or worker")
        for flag in ("enabled", "public"):
            if type(entry[flag]) is not bool:
                raise ValueError("Deployment flags must be boolean")
        if entry["public"] and not entry["enabled"]:
            raise ValueError("Public sources must be enabled")
        if source.dataset == "election_results":
            if source.contests and list(source.contests) != entry.get("races"):
                raise ValueError("Catalog races differ from the source's explicit contest contract")
            if entry["worker"] == "manual":
                age = entry.get("manual_max_age_seconds", 21600)
                if type(age) is not int or not 900 <= age <= 604800:
                    raise ValueError("Invalid manual snapshot freshness limit")
            owners.setdefault(source.state, []).append((name, entry))
        elif entry["enabled"] or entry["public"]:
            raise ValueError("Ancillary sources run manually")
        if not IMAGE.fullmatch(entry["image_id"]) or not REVISION.fullmatch(entry["revision"]):
            raise ValueError("Sources require immutable image IDs and Git revisions")
        if not Path(entry["release_path"]).is_absolute():
            raise ValueError("Release paths must be absolute")
        if entry["status"] not in ("test", "unofficial"):
            raise ValueError("Publication status must be explicit")
    for entries in owners.values():
        if len(entries) == 1:
            if entries[0][1].get("replaces"):
                raise ValueError("Replacement target is missing")
            continue
        replacements = [(name, entry) for name, entry in entries if entry.get("replaces")]
        if len(entries) != 2 or len(replacements) != 1:
            raise ValueError("A state can have only one results owner and one explicit replacement")
        name, replacement = replacements[0]
        old_name, old = next(pair for pair in entries if pair[0] != name)
        if (
            replacement["replaces"] != old_name
            or replacement["worker"] != "expansion"
            or replacement["source"]["election_date"] != old["source"]["election_date"]
            or not expands_coverage(old, replacement)
            or sum(entry["public"] for _, entry in entries) > 1
        ):
            raise ValueError("Invalid coverage replacement or multiple public results owners")
    expected = sorted(name for name, e in sources.items() if e["worker"] == "baseline")
    if expected != sorted(baseline["source_ids"]):
        raise ValueError("Baseline source selection changed")
    return value


def read(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


@contextmanager
def locked(path, *, exclusive=False, nonblocking=False):
    """Linux host control lock; readers in the dashboard need no writable lock file."""
    import fcntl

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle, flags | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write(path, value):
    validate(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".catalog-", suffix=".tmp")
    try:
        # Set permissions before replacement, avoiding a brief unreadable live file.
        os.chmod(temporary, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update(path, change):
    with locked(path, exclusive=True):
        value = read(path)
        change(value)
        value["generation"] += 1
        write(path, value)
        return value


def entry(source, settings, *, image_id, revision, release_path, worker, status):
    return {
        "source": asdict(source),
        "settings": asdict(settings),
        "image_id": image_id,
        "revision": revision,
        "release_path": str(Path(release_path).resolve()),
        "worker": worker,
        "status": status,
        "enabled": worker == "baseline",
        "public": worker == "baseline",
    }


def sources(value):
    return {name: source_from_json(e["source"]) for name, e in value["sources"].items()}


def public_states(value):
    return {e["source"]["state"] for e in value["sources"].values() if e["public"]}


def prospective(value, names):
    """Select replacement snapshots without changing a running legacy collector."""
    result = json.loads(json.dumps(value))
    for name in names:
        entry = result["sources"][name]
        if (
            entry["worker"] not in ("expansion", "manual")
            or entry["source"]["dataset"] != "election_results"
        ):
            raise ValueError("Only managed result sources may be activated")
        if old_name := entry.get("replaces"):
            old = result["sources"][old_name]
            old["public"] = False
            if old["worker"] != "baseline":
                old["enabled"] = False
        entry.update(public=True, enabled=True)
    return validate(result)


def schemas(value):
    return sorted(
        {"ops", "landing", "serving", *(s.state.lower() for s in sources(value).values())}
    )


def verify_baseline(value, prod):
    names = sorted(name.strip() for name in prod["PIPELINE_SOURCES"].split(",") if name.strip())
    if names != sorted(value["baseline"]["source_ids"]):
        raise ValueError("The existing worker's PIPELINE_SOURCES changed")
    if prod["IMAGE_TAG"] != value["baseline"]["revision"]:
        raise ValueError("The existing worker's IMAGE_TAG changed")
