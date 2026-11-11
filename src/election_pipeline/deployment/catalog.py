"""The credential-free deployment state shared by the worker, backups, monitor and dashboard.

One release (an immutable Git checkout and the image built from it) runs every source.
Each source records only how it is deployed; its definition and races come from the
release's own ``config/sources.toml`` and ``election_pipeline.states``::

    {
      "version": 2,
      "generation": 7,
      "release": {"revision": ..., "image_id": ..., "release_path": ...},
      "previous_release": {...} | null,
      "dashboard": {"name": ..., "url": ..., "states": [...], ...},
      "previous_dashboard": {...},          # optional, the one-step rollback
      "sources": {
        "<source_id>": {
          "enabled": true, "public": true, "status": "unofficial",
          "acquisition": "http" | "manual",
          "paused": true,                     # optional: not collected, still served
          ...                                 # publication and manual-snapshot receipts
        }
      }
    }
"""

from __future__ import annotations

import functools
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from election_pipeline.settings import load_config
from election_pipeline.states import STATES

IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
CONFIG = Path(__file__).resolve().parents[3] / "config/sources.toml"


def configured():
    # The dashboard validates the catalog on every request; parse each version of the
    # configuration once.
    stat = CONFIG.stat()
    return dict(parsed(CONFIG, stat.st_mtime_ns, stat.st_size))


@functools.lru_cache(maxsize=4)
def parsed(path, modified, size):
    return load_config(path)[1]


def validate_release(release):
    if (
        not IMAGE.fullmatch(release.get("image_id", ""))
        or not REVISION.fullmatch(release.get("revision", ""))
        or not Path(release.get("release_path", "")).is_absolute()
    ):
        raise ValueError("A release needs an immutable image ID, Git revision and absolute path")


def validate(value):
    if value.get("version") != 2 or type(value.get("generation")) is not int:
        raise ValueError("Unsupported deployment catalog")
    validate_release(value["release"])
    if value.get("previous_release") is not None:
        validate_release(value["previous_release"])
    sources = configured()
    for name, entry in value["sources"].items():
        if name not in sources:
            raise ValueError(f"Catalog names an unconfigured source: {name}")
        for flag in ("enabled", "public"):
            if type(entry[flag]) is not bool:
                raise ValueError("Deployment flags must be boolean")
        if entry["public"] and not entry["enabled"]:
            raise ValueError("Public sources must be enabled")
        if entry["status"] not in ("test", "unofficial", "official"):
            raise ValueError("Publication status must be explicit")
        if entry["acquisition"] not in ("http", "manual"):
            raise ValueError("Acquisition must be http or manual")
        if type(entry.get("paused", False)) is not bool:
            raise ValueError("The paused flag must be boolean")
        if entry.get("paused") and (not entry["enabled"] or entry["acquisition"] != "http"):
            raise ValueError("Only an enabled HTTP source can be paused")
        source = sources[name]
        if source.dataset != "election_results":
            if entry["enabled"]:
                raise ValueError("Ancillary datasets are collected manually")
        elif name != STATES[source.state].source_id:
            raise ValueError(f"{name} is not its state's results source")
        if entry["acquisition"] == "manual":
            age = entry.get("manual_max_age_seconds", 21600)
            if type(age) is not int or not 900 <= age <= 604800:
                raise ValueError("Invalid manual snapshot freshness limit")
            if type(entry.get("manual_final", False)) is not bool:
                raise ValueError("Manual final flag must be boolean")
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


def public_states(value):
    sources = configured()
    return {
        sources[name].state
        for name, entry in value["sources"].items()
        if entry["public"] and sources[name].dataset == "election_results"
    }


def published(value, names):
    """The catalog with ``names`` made public, as a dashboard candidate will serve it."""
    result = json.loads(json.dumps(value))
    for name in names:
        result["sources"][name].update(enabled=True, public=True)
    return validate(result)
