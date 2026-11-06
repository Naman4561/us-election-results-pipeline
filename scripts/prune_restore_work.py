"""Preview or remove completed S3 restore work directories."""

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values

WORK = re.compile(r"restore-[a-z0-9_]{8}")
BACKUP = re.compile(r"backup_[0-9a-f]{32}")


def read(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def passed_restores(report_dir):
    verified = set()
    for path in report_dir.glob("election_restore_*_test.json"):
        try:
            report = read(path)
            if report["outcome"] == "passed" and report["temporary_database_removed"] is True:
                verified.add(Path(report["backup_dir"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return verified


def available_in_s3(config, receipt):
    backup_id = receipt["backup_id"]
    if (
        not BACKUP.fullmatch(backup_id)
        or receipt.get("schema_version") != 1
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", ""))
        or type(receipt.get("bytes")) is not int
        or receipt["bytes"] <= 0
    ):
        return False
    bucket = config["BACKUP_BUCKET"]
    prefix = config.get("BACKUP_PREFIX", "election-pipeline")
    key = f"{prefix}/rolling/{backup_id}"
    command = [
        "/usr/local/bin/aws",
        "--profile",
        config.get("AWS_PROFILE", "election-backup"),
        "--region",
        config.get("AWS_DEFAULT_REGION", "us-east-2"),
        "--cli-connect-timeout",
        "5",
        "--cli-read-timeout",
        "10",
    ]

    def aws(*args):
        return subprocess.check_output(
            [*command, *args], stderr=subprocess.DEVNULL, text=True, timeout=25
        )

    remote = json.loads(
        aws("s3", "cp", f"s3://{bucket}/{key}/complete.json", "-", "--only-show-errors")
    )
    if remote != receipt:
        return False
    size = aws(
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        f"{key}/bundle.zip",
        "--query",
        "ContentLength",
        "--output",
        "text",
    )
    return int(size) == receipt["bytes"]


def prune(destination, report_dir, config, *, apply=False):
    if destination.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    root = destination.resolve(strict=True)
    passed = passed_restores(report_dir)
    eligible, kept = [], {}
    for work in sorted(root.glob("restore-*")):
        if (
            not WORK.fullmatch(work.name)
            or work.is_symlink()
            or not work.is_dir()
            or work / "bundle" not in passed
        ):
            kept[work.name] = "No matching passed restore report"
            continue
        try:
            available = available_in_s3(config, read(work / "complete.json"))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            available = False
        if not available:
            kept[work.name] = "S3 backup could not be verified"
            continue
        if apply:
            shutil.rmtree(work)
        eligible.append(work.name)
    return {
        "outcome": "cleaned" if apply else "planned",
        "removed" if apply else "would_remove": eligible,
        "kept": kept,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    prod = dotenv_values("/etc/election-pipeline/production.env", interpolate=False)
    config = dotenv_values("/etc/election-pipeline/backup.env", interpolate=False)
    result = prune(
        Path("/srv/election-pipeline/backups"),
        Path(prod["DATA_DIR"]) / "recovery_checks",
        config,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
