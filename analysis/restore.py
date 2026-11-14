"""Copy the newest uploaded backup's database into the local analysis database.

Read-only on the server: it reads the backup's record and streams ``database.dump`` out
of its bundle over SSH, checks the dump against the backup manifest's SHA-256, then
replaces only the pipeline's own schemas locally. Outside data (``ref``, ``finance``,
``census``, ``history``) and the analysis models are kept; ``ref.restores`` records
which backup the copy is, so a published number can name the data it came from.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from psycopg import sql

from analysis import local

HOST = os.environ.get("ANALYSIS_SSH_HOST", "election-prod")
BACKUPS = "/srv/election-pipeline/backups"
# Run on the server with python3 -I; prints the newest upload's record. The manifest is
# large (its file index), so only what a restore needs leaves the server.
DESCRIBE = f"""
import json, pathlib
root = pathlib.Path({BACKUPS!r})
latest = json.loads((root / "latest-backup.json").read_text())
if latest.get("outcome") != "uploaded":
    raise SystemExit("The newest backup was not uploaded")
manifest = json.loads((root / latest["backup_id"] / "manifest.json").read_text())
try:
    check = json.loads((root / "latest-check-restore.json").read_text())
except OSError:
    check = {{}}
print(json.dumps({{
    "backup_id": latest["backup_id"],
    "finished_at": latest["finished_at"],
    "schemas": manifest["database_schemas"],
    "dump": manifest["files"]["database.dump"],
    "restore_checked": check.get("outcome") == "passed"
        and check.get("backup_id") == latest["backup_id"],
}}))
"""
STREAM = f"""
import pathlib, re, shutil, sys, zipfile
backup = sys.argv[1]
if not re.fullmatch(r"backup_[0-9a-f]{{32}}", backup):
    raise SystemExit("Invalid backup ID")
with zipfile.ZipFile(pathlib.Path({BACKUPS!r}) / backup / "bundle.zip") as bundle:
    with bundle.open("database.dump") as dump:
        shutil.copyfileobj(dump, sys.stdout.buffer, 1 << 20)
"""


def remote(script, *args, stdout=subprocess.PIPE):
    return subprocess.run(
        ["ssh", HOST, "sudo", "python3", "-I", "-", *args],
        input=script.encode(),
        stdout=stdout,
        stderr=subprocess.PIPE,
        check=True,
    )


def describe():
    return json.loads(remote(DESCRIBE).stdout)


def download(backup):
    """The backup's dump under ANALYSIS_HOME, downloaded once and checked."""
    target = local.HOME / "backups" / backup["backup_id"] / "database.dump"
    expected = backup["dump"]
    if target.exists() and verified(target, expected):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    with partial.open("wb") as handle:
        remote(STREAM, backup["backup_id"], stdout=handle)
    if not verified(partial, expected):
        partial.unlink()
        raise ValueError("The downloaded dump does not match the backup manifest")
    partial.replace(target)
    return target


def verified(path, expected):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return path.stat().st_size == expected["bytes"] and digest.hexdigest() == expected["sha256"]


def restore(dump, backup, db):
    """Replace the pipeline's schemas with the dump's, in one transaction."""
    with local.connect(db, autocommit=True) as conn:
        for schema in backup["schemas"]:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    with Path(dump).open("rb") as handle:
        result = subprocess.run(
            [
                "docker", "exec", "-i", local.CONTAINER, "pg_restore",
                "--username", db["user"], "--dbname", local.DATABASE,
                "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction",
            ],
            stdin=handle,
            capture_output=True,
        )  # fmt: skip
    if result.returncode:
        raise RuntimeError("pg_restore failed: " + result.stderr.decode(errors="replace")[-2000:])
    with local.connect(db) as conn:
        conn.execute(Path(__file__).with_name("sql").joinpath("ref.sql").read_text("utf-8"))
        conn.execute(
            "INSERT INTO ref.restores (backup_id, backup_finished_at, dump_sha256, restore_checked)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (backup_id) DO UPDATE"
            " SET restored_at = now(), restore_checked = EXCLUDED.restore_checked",
            (
                backup["backup_id"],
                backup["finished_at"],
                backup["dump"]["sha256"],
                backup["restore_checked"],
            ),
        )


def main():
    backup = describe()
    print(f"Newest backup: {backup['backup_id']}, finished {backup['finished_at']}")
    if not backup["restore_checked"]:
        print("Note: the last passed restore check on the server was of another backup.")
    db = local.start()
    dump = download(backup)
    print(f"Dump verified ({backup['dump']['bytes'] / 1e6:.0f} MB); restoring")
    restore(dump, backup, db)
    print("Restored; recorded in ref.restores")
