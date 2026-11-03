"""Back up production to S3 and rehearse restoration from a fresh download."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.recovery import (
    backup,
    check_restore,
    safe_child,
    verify_bundle,
)
from election_pipeline.storage import atomic_json, file_hash, utc_now

ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = r"backup_[0-9a-f]{32}"


def aws_copy(config, source, destination):
    result = subprocess.run(
        [
            "/usr/local/bin/aws",
            "--profile",
            config["AWS_PROFILE"],
            "--region",
            config["AWS_DEFAULT_REGION"],
            "s3",
            "cp",
            str(source),
            str(destination),
            "--only-show-errors",
            "--no-progress",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("S3 transfer failed: " + result.stderr[-2000:])


def pack_bundle(bundle, deployed_tag):
    manifest = verify_bundle(bundle)
    manifest["deployment"] = {
        "image_tag": deployed_tag,
        "maintenance_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
        ).strip(),
    }

    for name in ("Dockerfile", "compose.production.yml", "README.md"):
        target = bundle / name
        shutil.copy2(ROOT / name, target)
        manifest["files"][name] = {
            "bytes": target.stat().st_size,
            "sha256": file_hash(target),
        }

    atomic_json(bundle / "manifest.json", manifest)
    paths = ["manifest.json", *sorted(manifest["files"])]
    archive = bundle / "bundle.zip"

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
        for name in paths:
            zipped.write(safe_child(bundle, name), arcname=name)

    return archive, {
        "schema_version": 1,
        "backup_id": bundle.name,
        "created_at": utc_now(),
        "sha256": file_hash(archive),
        "bytes": archive.stat().st_size,
        "expanded_bytes": sum((bundle / name).stat().st_size for name in paths),
    }


def upload_bundle(config, bundle, archive, receipt):
    location = f"s3://{config['BACKUP_BUCKET']}/{config['BACKUP_PREFIX']}/rolling/{bundle.name}/"
    receipt_path = bundle / "upload.json"
    atomic_json(receipt_path, receipt)

    aws_copy(config, archive, location + "bundle.zip")

    # Uploaded last: a partial upload has no completion record.
    aws_copy(config, receipt_path, location + "complete.json")
    atomic_json(bundle / "uploaded.json", receipt)


def unpack_bundle(archive, destination, receipt):
    if (
        receipt.get("schema_version") != 1
        or not re.fullmatch(ID_PATTERN, receipt.get("backup_id", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", ""))
        or archive.stat().st_size != receipt.get("bytes")
        or file_hash(archive) != receipt["sha256"]
    ):
        raise ValueError("Downloaded archive does not match its completion record")

    destination.mkdir()

    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        names = [member.filename for member in members]

        if len(names) != len(set(names)) or sum(
            member.file_size for member in members
        ) != receipt.get("expanded_bytes"):
            raise ValueError("Unexpected archive entries or expanded size")

        for member in members:
            target = safe_child(destination, member.filename)
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Backup archive must contain regular files")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)

    manifest = verify_bundle(destination)
    if set(names) != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("Archive files do not match the verified manifest")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "check-restore"))
    parser.add_argument("--backup-id")
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/election-pipeline/production.env"),
    )
    parser.add_argument(
        "--backup-env-file",
        type=Path,
        default=Path("/etc/election-pipeline/backup.env"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("/srv/election-pipeline/backups"),
    )
    args = parser.parse_args()

    if args.action == "check-restore" and not re.fullmatch(ID_PATTERN, args.backup_id or ""):
        parser.error("check-restore requires --backup-id backup_<32 hexadecimal characters>")

    os.umask(0o077)
    prod = dict(dotenv_values(args.env_file, interpolate=False))
    config = dict(dotenv_values(args.backup_env_file, interpolate=False))
    config.setdefault("AWS_PROFILE", "election-backup")
    config.setdefault("AWS_DEFAULT_REGION", "us-east-2")
    config.setdefault("BACKUP_PREFIX", "election-pipeline")

    secrets = [value for name, value in prod.items() if "PASSWORD" in name and value]
    if config.get("BACKUP_BUCKET"):
        secrets.append(config["BACKUP_BUCKET"])

    started = time.monotonic()
    result = {"outcome": "running", "action": args.action}

    try:
        if not config.get("BACKUP_BUCKET") or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9/_-]*",
            config["BACKUP_PREFIX"],
        ):
            raise ValueError("Set BACKUP_BUCKET and a valid BACKUP_PREFIX in backup.env")

        db = {
            "host": prod.get("POSTGRES_HOST", "127.0.0.1"),
            "port": int(prod.get("POSTGRES_PORT", "5432")),
            "dbname": prod["POSTGRES_DB"],
            "user": prod["POSTGRES_USER"],
            "password": prod["POSTGRES_PASSWORD"],
            "connect_timeout": 10,
        }
        compose = (
            "--env-file",
            str(args.env_file),
            "--file",
            str(ROOT / "compose.production.yml"),
        )
        args.destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = Path(prod["DATA_DIR"])

        if args.action == "backup":
            from election_pipeline.deployment import catalog as registry

            with registry.locked(args.catalog_file) if args.catalog_file else nullcontext():
                catalog = registry.read(args.catalog_file) if args.catalog_file else None
                result = backup(
                    ROOT,
                    data,
                    ROOT / "config/sources.toml",
                    db,
                    args.destination,
                    "db",
                    compose_args=compose,
                    catalog=catalog,
                )
            bundle = Path(result["backup_dir"])
            result["backup_id"] = bundle.name
            archive, receipt = pack_bundle(bundle, prod["IMAGE_TAG"])
            upload_bundle(config, bundle, archive, receipt)
            result = {
                "outcome": "uploaded",
                "backup_id": bundle.name,
                "archive_bytes": receipt["bytes"],
            }

        else:
            work = Path(tempfile.mkdtemp(prefix="restore-", dir=args.destination))
            location = (
                f"s3://{config['BACKUP_BUCKET']}/"
                f"{config['BACKUP_PREFIX']}/rolling/"
                f"{args.backup_id}/"
            )
            aws_copy(
                config,
                location + "complete.json",
                work / "complete.json",
            )
            receipt = json.loads((work / "complete.json").read_text())
            if receipt.get("backup_id") != args.backup_id:
                raise ValueError("Downloaded completion record has the wrong backup ID")

            aws_copy(config, location + "bundle.zip", work / "bundle.zip")
            unpack_bundle(work / "bundle.zip", work / "bundle", receipt)

            result = check_restore(
                ROOT,
                db,
                work / "bundle",
                "db",
                compose_args=compose,
                writer_role=prod["PIPELINE_DB_USER"],
                reader_role=prod["DASHBOARD_DB_USER"],
                report_dir=data / "recovery_checks",
            )
            result["backup_id"] = args.backup_id

    except Exception as error:
        result.update(outcome="failed", error=str(error))

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["finished_at"] = utc_now()

    text = json.dumps(result, indent=2)
    for secret in secrets:
        text = text.replace(secret, "[redacted]")

    if args.destination.is_dir():
        atomic_json(
            args.destination / f"latest-{args.action}.json",
            json.loads(text),
        )

    print(text, flush=True)
    return 0 if result["outcome"] in ("uploaded", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
