"""Run only newly owned containers; never recreate the production Compose project."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from election_pipeline.deployment.catalog import IMAGE


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
    digest = hashlib.sha256()
    package = Path(path) / "src/election_pipeline"
    for file in sorted(package.rglob("*.py")):
        digest.update(file.relative_to(package).as_posix().encode() + b"\0")
        digest.update(file.read_bytes() + b"\0")
    return digest.hexdigest()


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
    cpus, memory = config.get("EXPANSION_CPUS", ""), config.get("EXPANSION_MEMORY", "")
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?)", cpus) or not 0 < float(cpus) <= 8:
        raise ValueError("Set EXPANSION_CPUS to a measured CPU budget between 0 and 8")
    if not re.fullmatch(r"[1-9][0-9]*[mg]", memory, re.I):
        raise ValueError("Set EXPANSION_MEMORY after checking host headroom, e.g. 768m")
    return ["--cpus", cpus, "--memory", memory, "--memory-swap", memory, "--pids-limit", "256"]


def base_args(name, image, config, *, network=True):
    if not name.startswith("election-expansion-") or not IMAGE.fullmatch(image):
        raise ValueError(
            "Only uniquely named expansion containers and immutable images are allowed"
        )
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


def run_job(entry, prod, config, arguments, *, timeout=240):
    name = "election-expansion-job-" + uuid.uuid4().hex
    env = {**database_env(prod), "HOME": "/tmp"}
    args = [
        *base_args(name, entry["image_id"], config),
        "--rm",
        "--mount",
        f"type=bind,src={prod['DATA_DIR']},dst=/app/data",
        *[part for key in env for part in ("--env", key)],
        entry["image_id"],
        "python",
        *arguments,
    ]
    try:
        return command(args, timeout=timeout, env={**os.environ, **env})
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Expansion job exceeded its timeout; forced removal requested for {name}"
        ) from error
    finally:
        # Killing a Docker client does not kill its container. Touch only this owned UUID.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False)


def metadata(image, source_id):
    return json.loads(
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                image,
                "python",
                "scripts/release_metadata.py",
                "--source",
                source_id,
            ]
        )
    )
