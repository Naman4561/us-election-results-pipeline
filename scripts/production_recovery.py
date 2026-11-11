"""Back up production to S3 and rehearse restoration from a fresh download.

``fetch`` downloads a backup with every pack it names and rebuilds its data directory,
for a real restore.
"""

import argparse
import json
import os
import random
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.recovery import (
    backup,
    check_restore,
    pack_kind,
    safe_child,
    unpack_pack,
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


def pack_bundle(bundle, revision):
    manifest = verify_bundle(bundle)
    manifest["deployment"] = {
        "release_revision": revision,
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


def packs_location(config):
    return f"s3://{config['BACKUP_BUCKET']}/{config['BACKUP_PREFIX']}/objects/packs/"


def upload_packs(config, bundle, names):
    """Upload a backup's new packs. They go first: the bundle uploaded after names them."""
    for name in names:
        aws_copy(config, bundle / "packs" / name, packs_location(config) + name)


def remote_packs(config):
    """{pack name: bytes} of every uploaded pack."""
    prefix = f"{config['BACKUP_PREFIX']}/objects/packs/"
    listing = aws_s3api(
        config, "list-objects-v2", "--bucket", config["BACKUP_BUCKET"], "--prefix", prefix
    )
    return {item["Key"][len(prefix) :]: item["Size"] for item in listing.get("Contents", [])}


def previous_manifest(destination):
    """The manifest of the newest uploaded backup still on disk, or None.

    Its packs were uploaded before it was, so the next backup can refer to them.
    """
    newest = None
    for bundle in Path(destination).iterdir():
        receipt = bundle / "uploaded.json"
        if re.fullmatch(ID_PATTERN, bundle.name) and receipt.is_file():
            created = json.loads(receipt.read_text(encoding="utf-8"))["created_at"]
            if newest is None or created > newest[0]:
                newest = (created, bundle)
    if newest is None:
        return None
    return json.loads((newest[1] / "manifest.json").read_text(encoding="utf-8"))


def download_bundle(config, backup_id, work):
    """Download and verify a backup's bundle into ``work/bundle``; returns its manifest."""
    location = f"s3://{config['BACKUP_BUCKET']}/{config['BACKUP_PREFIX']}/rolling/{backup_id}/"
    aws_copy(config, location + "complete.json", work / "complete.json")
    receipt = json.loads((work / "complete.json").read_text())
    if receipt.get("backup_id") != backup_id:
        raise ValueError("Downloaded completion record has the wrong backup ID")
    aws_copy(config, location + "bundle.zip", work / "bundle.zip")
    unpack_bundle(work / "bundle.zip", work / "bundle", receipt)
    return json.loads((work / "bundle" / "manifest.json").read_text(encoding="utf-8"))


def fetch_packs(config, manifest, work, *, everything=False):
    """Download a version 2 bundle's packs and extract them into ``work/bundle/data``.

    Every pack the manifest names must be in S3 at its recorded size. Every raw pack is
    extracted and each file verified; of the others, all of them when ``everything``,
    else the newest and one more chosen at random.
    """
    remote = remote_packs(config)
    for name, expected in manifest["packs"].items():
        if remote.get(name) != expected["bytes"]:
            raise ValueError("A pack is missing from S3 or has the wrong size: " + name)
    raw = sorted(n for n in manifest["packs"] if n.endswith("-raw.zip"))
    rest = sorted(n for n in manifest["packs"] if n not in raw)
    if not everything and len(rest) > 2:
        newest = max(rest, key=lambda n: manifest["packs"][n]["created_at"])
        rest = [newest, random.SystemRandom().choice([n for n in rest if n != newest])]
    packs = work / "packs"
    packs.mkdir()
    verified = {}
    for name in raw + rest:
        aws_copy(config, packs_location(config) + name, packs / name)
        verified[name] = len(unpack_pack(manifest, packs / name, work / "bundle" / "data"))
        (packs / name).unlink()
    wanted = [p for p, e in manifest["data"].items() if everything or pack_kind(p) == "raw"]
    if sum(verified[n] for n in verified if everything or n in raw) != len(wanted):
        raise ValueError("Packs did not hold every file the manifest assigns them")
    return {"packs_listed": len(manifest["packs"]), "packs_verified": verified}


KEPT_AFTER_UPLOAD = {"bundle.zip", "manifest.json", "upload.json", "uploaded.json"}


def trim_uploaded_bundle(bundle):
    """Remove the unpacked copy once its archive is in S3, keeping ``bundle.zip``.

    The archive with its receipt restores through ``unpack_bundle``; the unpacked tree only
    duplicated it (about 12 GB on election week). Returns the bytes freed.
    """
    receipt = json.loads((bundle / "uploaded.json").read_text(encoding="utf-8"))
    archive = bundle / "bundle.zip"
    if receipt.get("backup_id") != bundle.name or archive.stat().st_size != receipt["bytes"]:
        raise ValueError("The uploaded receipt does not match the local archive")
    freed = 0
    for entry in bundle.iterdir():
        if entry.name in KEPT_AFTER_UPLOAD:
            continue
        if entry.is_dir() and not entry.is_symlink():
            freed += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            shutil.rmtree(entry)
        else:
            freed += entry.lstat().st_size
            entry.unlink()
    return freed


def aws_s3api(config, *arguments):
    result = subprocess.run(
        [
            "/usr/local/bin/aws",
            "--profile",
            config["AWS_PROFILE"],
            "--region",
            config["AWS_DEFAULT_REGION"],
            "s3api",
            *arguments,
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("S3 request failed: " + result.stderr[-2000:])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def mark_expired_backups(config, state_path, current_id, now=None):
    """Tag remote backups outside the newest few as ``retention=expire``.

    The bucket's lifecycle rule deletes tagged objects once they are older than its
    expiry (3 days), so a backup survives while it is recent or among the newest
    ``BACKUP_S3_KEEP_LATEST``. Nothing is tagged unless a backup was just uploaded, so if
    backups stop the newest ones are kept indefinitely. The backup user needs
    ``s3:PutObjectTagging`` only; it cannot delete.
    """
    keep_latest = int(config.get("BACKUP_S3_KEEP_LATEST", "5"))
    if keep_latest < 1:
        raise ValueError("Keep at least one remote backup")
    now = now or datetime.now(timezone.utc)
    bucket, prefix = config["BACKUP_BUCKET"], f"{config['BACKUP_PREFIX']}/rolling/"
    listing = aws_s3api(config, "list-objects-v2", "--bucket", bucket, "--prefix", prefix)

    objects, completed = {}, {}
    for item in listing.get("Contents", []):
        backup_id, _, name = item["Key"][len(prefix) :].partition("/")
        if not re.fullmatch(ID_PATTERN, backup_id):
            continue
        modified = datetime.fromisoformat(item["LastModified"].replace("Z", "+00:00"))
        objects.setdefault(backup_id, []).append((item["Key"], modified))
        if name == "complete.json":
            completed[backup_id] = modified

    newest = sorted(completed, key=completed.get, reverse=True)[:keep_latest]
    if current_id not in newest:
        raise ValueError("The uploaded backup is missing from the remote listing")
    # An upload without a completion record is abandoned once it is a day old.
    stale = {
        backup_id
        for backup_id, items in objects.items()
        if backup_id not in completed and now - max(m for _, m in items) > timedelta(days=1)
    }
    expired = (set(completed) - set(newest)) | stale

    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    marked = set(state.get("marked", []))
    for backup_id in sorted(expired - marked):
        for key, _ in objects[backup_id]:
            aws_s3api(
                config,
                "put-object-tagging",
                "--bucket",
                bucket,
                "--key",
                key,
                "--tagging",
                "TagSet=[{Key=retention,Value=expire}]",
            )
        marked.add(backup_id)
        # Saved per backup so an interrupted run does not repeat finished work.
        atomic_json(state_path, {"marked": sorted(marked & set(objects))})
    atomic_json(state_path, {"marked": sorted(marked & set(objects))})
    return {
        "outcome": "applied",
        "kept": newest,
        "newly_marked": sorted(expired - set(state.get("marked", []))),
    }


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


def awaiting_restore(catalog_file, destination):
    """Sources a restore check would admit now: promoted but not yet public, in the latest
    uploaded backup (it finished after their publication), not yet verified by a passed
    restore check. Certified states added from their certified file are among them."""
    from election_pipeline.deployment import catalog as registry

    def record(name):
        try:
            return json.loads((destination / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    backup, checked = record("latest-backup.json"), record("latest-check-restore.json")
    if backup.get("outcome") != "uploaded" or not backup.get("finished_at"):
        return []
    verified = checked.get("current_publications", {}) if checked.get("outcome") == "passed" else {}
    return sorted(
        name
        for name, entry in registry.read(catalog_file)["sources"].items()
        if entry.get("publication_id")
        and not entry["public"]
        and entry.get("published_at")
        and datetime.fromisoformat(entry["published_at"])
        < datetime.fromisoformat(backup["finished_at"])
        and verified.get(name) != entry["publication_id"]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "check-restore", "fetch"))
    parser.add_argument("--backup-id")
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument(
        "--if-awaiting",
        action="store_true",
        help="check-restore only when a promoted state awaits one (see awaiting_restore)",
    )
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

    if args.action == "check-restore" and args.if_awaiting:
        if not args.catalog_file:
            parser.error("--if-awaiting reads the deployment: pass --catalog-file")
        awaiting = awaiting_restore(args.catalog_file, args.destination)
        if not awaiting:
            print(json.dumps({"outcome": "not_needed"}), flush=True)
            return 0
        print(json.dumps({"awaiting": awaiting}), flush=True)
    if args.action == "check-restore" and args.backup_id is None:
        # The check after a backup (election-check-restore.service) restores the newest upload.
        try:
            latest = json.loads((args.destination / "latest-backup.json").read_text("utf-8"))
        except (OSError, ValueError):
            latest = {}
        if latest.get("outcome") == "uploaded":
            args.backup_id = latest.get("backup_id")
    if args.action != "backup" and not re.fullmatch(ID_PATTERN, args.backup_id or ""):
        parser.error(f"{args.action} requires --backup-id backup_<32 hexadecimal characters>")

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

            if not args.catalog_file:
                raise ValueError("A backup records the deployment: pass --catalog-file")
            with registry.locked(args.catalog_file):
                catalog = registry.read(args.catalog_file)
                result = backup(
                    ROOT,
                    data,
                    ROOT / "config/sources.toml",
                    db,
                    args.destination,
                    "db",
                    compose_args=compose,
                    catalog=catalog,
                    previous=previous_manifest(args.destination),
                )
            bundle = Path(result["backup_dir"])
            result["backup_id"] = bundle.name
            new_packs = {n: (bundle / "packs" / n).stat().st_size for n in result["new_packs"]}
            try:
                upload_packs(config, bundle, new_packs)
                archive, receipt = pack_bundle(bundle, catalog["release"]["revision"])
                upload_bundle(config, bundle, archive, receipt)
            except Exception:
                # Nothing complete reached S3; the next run starts a fresh bundle.
                shutil.rmtree(bundle, ignore_errors=True)
                raise
            result = {
                "outcome": "uploaded",
                "backup_id": bundle.name,
                "archive_bytes": receipt["bytes"],
                "new_packs": new_packs,
            }
            try:
                result["local_bytes_freed"] = trim_uploaded_bundle(bundle)
            except Exception as error:
                result["local_trim"] = {"outcome": "failed", "error": str(error)[-500:]}
            # The backup is safe in S3 either way; the monitor reports a retention failure.
            try:
                result["s3_retention"] = mark_expired_backups(
                    config, args.destination / "s3-retention.json", bundle.name
                )
            except Exception as error:
                result["s3_retention"] = {"outcome": "failed", "error": str(error)[-500:]}

        elif args.action == "fetch":
            work = Path(tempfile.mkdtemp(prefix="fetch-", dir=args.destination))
            manifest = download_bundle(config, args.backup_id, work)
            result = {"outcome": "passed", "backup_id": args.backup_id}
            if manifest["schema_version"] == 2:
                result.update(fetch_packs(config, manifest, work, everything=True))
            result["restored_dir"] = str(work / "bundle")

        else:
            work = Path(tempfile.mkdtemp(prefix="restore-", dir=args.destination))
            manifest = download_bundle(config, args.backup_id, work)
            packs = fetch_packs(config, manifest, work) if manifest["schema_version"] == 2 else {}
            del manifest

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
            result.update(packs)
            if result.get("outcome") == "passed" and result.get("temporary_database_removed"):
                # The persistent restore report has already been written outside this directory.
                try:
                    shutil.rmtree(work)
                except OSError as error:
                    result.update(restore_work_removed=False, restore_cleanup_error=str(error))
                else:
                    result["restore_work_removed"] = True

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
