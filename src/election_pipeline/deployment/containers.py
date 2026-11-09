"""Containers this deployment owns: jobs, imports and dashboards from the release image.

The Compose project (PostgreSQL and Caddy) is never recreated from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from election_pipeline.deployment.catalog import IMAGE
from election_pipeline.pipeline import collector_code_hash


def command(args, *, timeout=30, env=None, input=None):
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False, env=env, input=input
    )
    if result.returncode:
        detail = result.stderr[-2000:]
        # Worker failure JSON is printed to stdout; preserve it for diagnosis.
        detail += "\n" + result.stdout[-4000:]
        raise RuntimeError(
            f"Command failed ({Path(args[0]).name}, exit {result.returncode}): {detail.strip()}"
        )
    return result.stdout.strip()


def inspect_image(image):
    value = json.loads(command(["docker", "image", "inspect", image]))[0]
    identity = value["Id"]
    if not IMAGE.fullmatch(identity):
        raise ValueError("Docker did not return an immutable image ID")
    return identity, value["Config"].get("Labels", {}).get("org.opencontainers.image.revision")


def verify_release(path, image):
    path = Path(path).resolve()
    revision = command(["git", "-C", str(path), "rev-parse", "HEAD"])
    dirty = command(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"])
    if dirty:
        raise ValueError("Candidate release must be committed and clean")
    identity, label = inspect_image(image)
    if label != revision:
        raise ValueError("Image revision differs from the release checkout")
    return identity, revision


def package_hash(path):
    return collector_code_hash(Path(path) / "src/election_pipeline")


def release_hash(path):
    """Fingerprint tracked files; Caddyfile is intentionally mutable runtime routing."""
    path = Path(path)
    tracked = command(["git", "-C", str(path), "ls-files", "-z"]).split("\0")
    digest = hashlib.sha256()
    for name in sorted(name for name in tracked if name and name != "deploy/Caddyfile"):
        file = path / name
        if file.is_symlink():
            raise ValueError("Registered releases must not contain symlinks")
        digest.update(name.encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def limits(config):
    cpus, memory = config.get("JOB_CPUS", ""), config.get("JOB_MEMORY", "")
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?)", cpus) or not 0 < float(cpus) <= 8:
        raise ValueError("Set JOB_CPUS to a measured CPU budget between 0 and 8")
    if not re.fullmatch(r"[1-9][0-9]*[mg]", memory, re.I):
        raise ValueError("Set JOB_MEMORY after checking host headroom, e.g. 768m")
    return ["--cpus", cpus, "--memory", memory, "--memory-swap", memory, "--pids-limit", "256"]


def base_args(name, image, config, *, network=True):
    if not re.fullmatch(r"election-(?:job|import|dashboard)-[0-9a-f]{32}", name) or not (
        IMAGE.fullmatch(image)
    ):
        raise ValueError("Only uniquely named owned containers and immutable images are allowed")
    return [
        "docker",
        "run",
        "--name",
        name,
        "--init",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=2",
        *limits(config),
        "--network",
        config.get("DOCKER_NETWORK", "election-production_default") if network else "none",
    ]


def database_env(prod, *, reader=False):
    prefix = "DASHBOARD_POSTGRES_" if reader else "POSTGRES_"
    return {
        prefix + "HOST": "db",
        prefix + "PORT": "5432",
        prefix + "DB": prod["POSTGRES_DB"],
        prefix + "USER": prod["DASHBOARD_DB_USER" if reader else "PIPELINE_DB_USER"],
        prefix + "PASSWORD": prod["DASHBOARD_DB_PASSWORD" if reader else "PIPELINE_DB_PASSWORD"],
    }


def run_job(image, prod, config, arguments, *, timeout=240):
    name = "election-job-" + uuid.uuid4().hex
    env = {**database_env(prod), "HOME": "/tmp"}
    args = [
        *base_args(name, image, config),
        "--rm",
        "--mount",
        f"type=bind,src={prod['DATA_DIR']},dst=/app/data",
        *[part for key in env for part in ("--env", key)],
        image,
        "python",
        *arguments,
    ]
    try:
        return command(args, timeout=timeout, env={**os.environ, **env})
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Job exceeded its timeout; forced removal requested for {name}"
        ) from error
    finally:
        # Killing a Docker client does not kill its container. Touch only this owned UUID.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False)


def import_county(image, source_id, prod, config, county, report, review):
    """An offline import needs the data mount, but no database credentials or network."""
    root = Path(prod["DATA_DIR"]).resolve()
    paths = []
    for original in (report, review):
        path = Path(original).resolve(strict=True)
        if not path.is_file():
            raise ValueError("County input must be a file within DATA_DIR")
        paths.append("/app/data/" + path.relative_to(root).as_posix())
    name = "election-import-" + uuid.uuid4().hex
    args = [
        *base_args(name, image, config, network=False),
        "--rm",
        "--mount",
        f"type=bind,src={root},dst=/app/data",
        image,
        "python",
        "-m",
        "election_pipeline",
        "import-county",
        "--source",
        source_id,
        "--county",
        county,
        "--data-dir",
        "/app/data",
        "--file",
        paths[0],
        "--review-json",
        paths[1],
    ]
    try:
        return command(args, timeout=120)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False)
