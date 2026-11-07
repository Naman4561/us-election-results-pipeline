"""Plan or apply retention for re-creatable working files under the data directory.

Kept forever, because backups and restore checks verify them or they are the
evidence trail: raw archives (``raw/``), fetch metadata (``metadata/``), quarantined
payloads (``quarantine/``), collector state, accuracy checks and recovery checks.

Pruned once older than ``--keep-days`` (by modification time):

- ``runs/<source>/<run>.json``: per-run reports (``latest.json`` is always kept);
- ``reports/{parsing,loading}/<source>/*.json``: failed parse and load reports;
- ``publications/<attempt>/``: dbt artifacts of publication attempts (the published
  tables themselves live in PostgreSQL);
- ``parsed/<source>/<sha>-<run>/``: parser outputs, except the one for the source's
  current snapshot, which every scheduled run reuses. Any other output can be
  re-created exactly from its raw archive.

A source's files are pruned only while holding that source's pipeline lock, so a
running worker never loses a file it is using. Without ``--apply`` nothing is deleted.
"""

import argparse
import json
import re
import shutil
import time
from pathlib import Path

from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore, CollectionLocked

ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PARSED = re.compile(r"[0-9a-f]{16}-[0-9a-f]{16}")


def old(path, cutoff):
    return not path.is_symlink() and path.stat().st_mtime < cutoff


def current_prefix(store):
    try:
        digest = store.read_state().get("sha256")
    except (OSError, ValueError):
        return None  # unreadable state: keep every parsed output for this source
    return digest[:16] if isinstance(digest, str) else None


def plan_source(data, store, cutoff):
    source = store.source.source_id
    planned = []
    runs = data / "runs" / source
    if runs.is_dir():
        planned += [p for p in runs.glob("*.json") if p.name != "latest.json" and old(p, cutoff)]
    for stage in ("parsing", "loading"):
        folder = data / "reports" / stage / source
        if folder.is_dir():
            planned += [p for p in folder.glob("*.json") if old(p, cutoff)]
    parsed = data / "parsed" / source
    if parsed.is_dir():
        keep = current_prefix(store)
        if keep is not None or not (data / "state" / f"{source}.json").exists():
            planned += [
                p
                for p in parsed.iterdir()
                if PARSED.fullmatch(p.name)
                and p.is_dir()
                and not p.name.startswith(f"{keep}-")
                and old(p, cutoff)
            ]
    return planned


def remove(path):
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def prune_data(data_dir, sources, keep_days=7, apply=False, now=None):
    if keep_days < 1:
        raise ValueError("Keep at least one day of working files")
    data = Path(data_dir)
    if data.is_symlink():
        raise ValueError("Data directory must not be a symlink")
    data = data.resolve(strict=True)
    cutoff = (now or time.time()) - keep_days * 86400
    result = {"apply": apply, "keep_days": keep_days, "removed": [], "skipped_locked": []}

    for source in sources.values():
        store = ArchiveStore(data, source)
        try:
            with store.lock(stage="pipeline"):
                planned = plan_source(data, store, cutoff)
                for path in planned:
                    if apply:
                        remove(path)
                result["removed"] += [str(p.relative_to(data)) for p in planned]
        except CollectionLocked:
            result["skipped_locked"].append(source.source_id)

    publications = data / "publications"
    if publications.is_dir():
        planned = [
            p
            for p in publications.iterdir()
            if ATTEMPT.fullmatch(p.name) and p.is_dir() and old(p, cutoff)
        ]
        for path in planned:
            if apply:
                shutil.rmtree(path)
        result["removed"] += [str(p.relative_to(data)) for p in planned]
    result["count"] = len(result["removed"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--config", type=Path, default=ROOT / "config/sources.toml")
    parser.add_argument("--keep-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="Delete; otherwise only plan")
    parser.add_argument("--list", action="store_true", help="Print every planned path")
    args = parser.parse_args()
    _, sources = load_config(args.config)
    result = prune_data(args.data_dir, sources, args.keep_days, args.apply)
    if not args.list:
        result["removed"] = result["removed"][:20]
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
