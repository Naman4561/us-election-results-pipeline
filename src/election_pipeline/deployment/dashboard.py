"""Start a candidate dashboard, check it, then point the existing Caddy process at it."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from election_pipeline.deployment import catalog, containers
from election_pipeline.storage import atomic_json, utc_now


def expected_races(value):
    """{state: races} that a dashboard for this catalog must serve."""
    from election_pipeline.states import STATES

    return {code: list(STATES[code].races) for code in catalog.public_states(value)}


def check(url, expected):
    with urllib.request.urlopen(url.rstrip("/") + "/api/national", timeout=10) as response:
        payload = json.load(response)
    if payload.get("mode") != "published" or set(payload.get("states", {})) != set(expected):
        raise ValueError("Candidate dashboard has incorrect coverage")
    for code, entry in expected.items():
        state = payload["states"][code]
        publication = state.get("publication") or {}
        if (
            state.get("availability") != "available"
            or not publication.get("publication_id")
            or set(state.get("races", {})) != set(entry)
        ):
            raise ValueError(f"Candidate dashboard is missing published results for {code}")
        if publication.get("status") not in ("test", "unofficial"):
            raise ValueError("Unexpected publication label")
    return payload


def start(value, names, prod, config, directory, *, port, release):
    """Start a private dashboard from ``release`` serving the catalog with ``names`` public."""
    if not 8001 <= port <= 8099:
        raise ValueError("Candidate port must be localhost 8001–8099")
    image_id, revision = release["image_id"], release["revision"]
    prospective = catalog.published(value, names)
    codes = catalog.public_states(prospective)
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o755)
    os.chmod(directory, 0o755)
    catalog.write(directory / "catalog.json", prospective)
    name = "election-dashboard-" + directory.name.rsplit("-", 1)[-1]
    env = {
        **containers.database_env(prod, reader=True),
        "DASHBOARD_CATALOG": "/coverage/catalog.json",
    }
    containers.command(
        [
            *containers.base_args(name, image_id, config),
            "--detach",
            "--restart",
            "unless-stopped",
            "--publish",
            f"127.0.0.1:{port}:8000",
            "--mount",
            f"type=bind,src={directory.resolve()},dst=/coverage,readonly",
            *[part for key in env for part in ("--env", key)],
            image_id,
            "python",
            "-m",
            "election_pipeline.dashboard.server",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ],
        env={**os.environ, **env},
    )
    return {
        "name": name,
        "url": f"http://127.0.0.1:{port}",
        "image_id": image_id,
        "revision": revision,
        "states": sorted(codes),
        "sources": names,
        "catalog_generation": value["generation"],
        "coverage_dir": str(directory.resolve()),
    }


def persist(path, content):
    # The existing deployment mounts a single Caddyfile. Preserve its inode so both
    # the current container and a replacement after reboot read the same bytes.
    with Path(path).open("r+", encoding="utf-8", newline="\n") as handle:
        handle.seek(0)
        handle.write(content)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def reload(caddy, content):
    containers.command(
        [
            "docker",
            "exec",
            "-i",
            caddy,
            "caddy",
            "reload",
            "--config",
            "-",
            "--adapter",
            "caddyfile",
        ],
        input=content,
    )


def handoff(path, caddy, target, journal, *, before=None, catalog_file=None):
    if not re.fullmatch(r"election-dashboard-[0-9a-f]{32}:8000", target):
        raise ValueError("Invalid dashboard upstream")
    old = Path(path).read_text(encoding="utf-8")
    found = re.findall(r"reverse_proxy\s+(\S+)", old)
    if len(found) != 3 or len(set(found)) != 1:
        raise ValueError("Review the Caddyfile: expected three identical dashboard upstreams")
    new = re.sub(r"(reverse_proxy\s+)\S+", lambda match: match[1] + target, old)
    record = {
        "outcome": "pending",
        "old": old,
        "new": new,
        "caddy": caddy,
        "path": str(Path(path).resolve()),
        "started_at": utc_now(),
    }
    if before is not None:
        record.update(catalog_before=before, catalog_file=str(catalog_file))
    atomic_json(Path(journal), record)
    try:
        # Caddy validates the new config and keeps the old one if reload fails.
        reload(caddy, new)
        persist(path, new)
    except Exception:
        reload(caddy, old)
        persist(path, old)
        atomic_json(Path(journal), {**record, "outcome": "rolled_back"})
        raise
    if before is None:
        atomic_json(Path(journal), {**record, "outcome": "applied"})
    return old


def recover(journal):
    """Explicit recovery after a host crash during the in-place persistence window."""
    record = json.loads(Path(journal).read_text(encoding="utf-8"))
    if record["outcome"] != "pending":
        raise ValueError("There is no pending Caddy handoff")
    reload(record["caddy"], record["old"])
    persist(record["path"], record["old"])
    if "catalog_before" in record:
        catalog.write(record["catalog_file"], record["catalog_before"])
    atomic_json(Path(journal), {**record, "outcome": "rolled_back"})


def stop_unused(name, value):
    protected = {
        d.get("name") for d in (value.get("dashboard", {}), value.get("previous_dashboard", {}))
    }
    if not re.fullmatch(r"election-dashboard-[0-9a-f]{32}", name) or name in protected:
        raise ValueError("Active and previous dashboard containers are protected")
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=True, timeout=20)
