"""Docker PostgreSQL backups and isolated restoration checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from psycopg import sql

from election_pipeline.database import connect
from election_pipeline.settings import load_config
from election_pipeline.storage import (
    ArchiveStore,
    atomic_json,
    file_hash,
    utc_now,
)

SCHEMAS = ("ops", "landing", "serving")

DATA_DIRECTORIES = (
    "raw",
    "metadata",
    "parsed",
    "state",
    "runs",
    "publications",
    "accuracy",
    "reports",
    "recovery_checks",
)


def safe_child(root, relative):
    """Reject absolute paths, traversal and symlinks."""
    parts = PurePosixPath(relative)

    if (
        not parts.parts
        or parts.is_absolute()
        or any(
            part in ("..", ".") or "\\" in part or ":" in part
            for part in parts.parts
        )
    ):
        raise ValueError("Unsafe backup path")

    result = Path(root).joinpath(*parts.parts)

    if any(path.is_symlink() for path in [result, *result.parents]):
        raise ValueError("Backup paths must not be symlinks")

    if not result.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Backup path escapes its directory")

    return result


def inventory(conn):
    """Fingerprint every business/audit table in one DB transaction."""
    tables = conn.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(%s)
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
        """,
        (list(SCHEMAS),),
    ).fetchall()

    found = {row["table_schema"] for row in tables}

    if not set(SCHEMAS).issubset(found):
        raise ValueError(
            "Apply the ingestion and publication migrations first"
        )

    result = {}

    for table in tables:
        digest = hashlib.sha256()
        count = 0

        query = sql.SQL(
            """
            SELECT to_jsonb(t)::text AS value
            FROM {} t
            ORDER BY to_jsonb(t)::text COLLATE "C"
            """
        ).format(
            sql.Identifier(
                table["table_schema"],
                table["table_name"],
            )
        )

        with conn.cursor(name="backup_" + uuid.uuid4().hex) as cursor:
            cursor.execute(query)

            for row in cursor:
                digest.update(row["value"].encode("utf-8") + b"\n")
                count += 1

        name = table["table_schema"] + "." + table["table_name"]
        result[name] = {
            "rows": count,
            "sha256": digest.hexdigest(),
        }

    return result


def docker_command(
    root,
    service,
    db,
    program,
    arguments,
    *,
    stdin=None,
    stdout=None,
):
    """Stream binary files without a shell or password arguments."""
    if not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]{0,62}", db["dbname"]
    ):
        raise ValueError(
            "Backup expects a simple PostgreSQL database name"
        )

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service):
        raise ValueError("Invalid Compose service name")

    command = [
        "docker", "compose", "exec", "-T", service, program
    ]

    needs_connection = (
        program == "pg_dump"
        or (program == "pg_restore" and "--list" not in arguments)
    )

    if needs_connection:
        command += [
            "--host=/var/run/postgresql",
            "--port=5432",
            "--no-password",
            "--username=" + db["user"],
            "--dbname=" + db["dbname"],
        ]

    result = subprocess.run(
        [*command, *arguments],
        cwd=root,
        stdin=stdin,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=1800,
        check=False,
    )

    message = result.stderr.decode(
        "utf-8", errors="replace"
    ).replace(db["password"], "[redacted]")

    if result.returncode:
        raise ValueError(
            f"{program} failed: {message[-4000:]}"
        )

    return message.strip()


def copy_files(source, destination):
    if not source.exists():
        return

    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)

        if (
            any(part.startswith(".") for part in relative.parts)
            or path.suffix in (".part", ".tmp", ".pyc")
            or "__pycache__" in relative.parts
        ):
            continue

        if path.is_symlink():
            raise ValueError(
                f"Refusing symlink in backup: {relative}"
            )

        if path.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def backup(root, data_dir, config, db, destination, service):
    """Capture files and a dump using the inventory's DB snapshot."""
    root = Path(root).resolve()
    data_dir = Path(data_dir).resolve()
    destination = Path(destination).resolve()

    if (
        destination.is_relative_to(data_dir)
        or data_dir.is_relative_to(destination)
    ):
        raise ValueError(
            "Choose a backup destination outside the live data directory"
        )

    _, sources = load_config(config)

    identity = "backup_" + uuid.uuid4().hex

    # The manifest written last marks this bundle as complete.
    temporary = destination / identity
    temporary.mkdir(parents=True, exist_ok=False)

    manifest = {
        "schema_version": 1,
        "started_at": utc_now(),
        "database": db["dbname"],
    }

    with ExitStack() as stack:
        for source in sorted(
            sources.values(), key=lambda item: item.source_id
        ):
            store = ArchiveStore(data_dir, source)
            stack.enter_context(store.lock(stage="pipeline"))
            stack.enter_context(store.lock())

        conn = stack.enter_context(connect(db))

        locked = conn.execute(
            "SELECT pg_try_advisory_lock(20261103, 1) AS ok"
        ).fetchone()["ok"]

        if not locked:
            raise ValueError(
                "Publication or restore is running; retry backup later"
            )

        stack.enter_context(conn.transaction())
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )

        snapshot = conn.execute(
            "SELECT pg_export_snapshot() AS id"
        ).fetchone()["id"]

        manifest["database_inventory"] = inventory(conn)

        for directory in DATA_DIRECTORIES:
            copy_files(
                data_dir / directory,
                temporary / "data" / directory,
            )

        snapshots = conn.execute(
            "SELECT raw_path, raw_sha256 FROM ops.snapshots"
        ).fetchall()

        for row in snapshots:
            archived = safe_child(
                temporary / "data", row["raw_path"]
            )

            if (
                not archived.is_file()
                or file_hash(archived) != row["raw_sha256"]
            ):
                raise ValueError(
                    "Missing/corrupt raw archive for snapshot: "
                    + row["raw_sha256"]
                )

        shutil.copy2(config, temporary / "sources.toml")

        for directory in ("src", "scripts", "migrations"):
            copy_files(
                root / directory,
                temporary / "code" / directory,
            )

        for directory in ("models", "macros", "tests"):
            copy_files(
                root / "dbt" / directory,
                temporary / "code/dbt" / directory,
            )

        for path in (root / "dbt").glob("*.yml"):
            if not path.name.startswith("."):
                shutil.copy2(
                    path, temporary / "code/dbt" / path.name
                )

        for filename in ("pyproject.toml", "requirements-dev.txt"):
            if (root / filename).is_file():
                shutil.copy2(
                    root / filename, temporary / filename
                )

        dump = temporary / "database.dump"

        with dump.open("wb") as handle:
            warning = docker_command(
                root,
                service,
                db,
                "pg_dump",
                [
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--snapshot=" + snapshot,
                    *["--schema=" + schema for schema in SCHEMAS],
                ],
                stdout=handle,
            )
            handle.flush()
            os.fsync(handle.fileno())

        if warning:
            manifest["dump_warnings"] = warning

        with dump.open("rb") as handle:
            docker_command(
                root,
                service,
                db,
                "pg_restore",
                ["--list"],
                stdin=handle,
            )

    manifest["finished_at"] = utc_now()
    manifest["files"] = {
        path.relative_to(temporary).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": file_hash(path),
        }
        for path in sorted(temporary.rglob("*"))
        if path.is_file()
    }

    atomic_json(temporary / "manifest.json", manifest)

    return {
        "outcome": "backed_up",
        "backup_dir": str(temporary),
        "files": len(manifest["files"]),
    }


def verify_bundle(directory):
    directory = Path(directory).resolve()

    manifest = json.loads(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )

    if (
        manifest.get("schema_version") != 1
        or "database.dump" not in manifest.get("files", {})
    ):
        raise ValueError("Unsupported/incomplete backup manifest")

    for relative, expected in manifest["files"].items():
        path = safe_child(directory, relative)

        if (
            path.stat().st_size != expected["bytes"]
            or file_hash(path) != expected["sha256"]
        ):
            raise ValueError(
                f"Backup checksum failed: {relative}"
            )

    return manifest


def check_restore(root, db, directory, service):
    """Restore into this call's new temporary database, then remove it."""
    directory = Path(directory).resolve()
    manifest = verify_bundle(directory)

    name = "election_restore_" + uuid.uuid4().hex[:16] + "_test"
    temporary_db = {**db, "dbname": name}

    report = {
        "outcome": "running",
        "started_at": utc_now(),
        "temporary_database": name,
        "backup_dir": str(directory),
    }

    path = Path(root) / "data/recovery_checks" / f"{name}.json"
    created = False

    try:
        with connect({**db, "dbname": "postgres"}) as admin:
            admin.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0"
                ).format(sql.Identifier(name))
            )
            created = True

        with (directory / "database.dump").open("rb") as handle:
            docker_command(
                root,
                service,
                temporary_db,
                "pg_restore",
                [
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-privileges",
                ],
                stdin=handle,
            )

        with connect(temporary_db) as restored, restored.transaction():
            restored.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )

            actual = inventory(restored)

            if actual != manifest["database_inventory"]:
                raise ValueError(
                    "Restored table counts/hashes do not match the backup snapshot"
                )

            snapshots = restored.execute(
                "SELECT raw_path, raw_sha256 FROM ops.snapshots"
            ).fetchall()

            for snapshot in snapshots:
                raw = safe_child(
                    directory / "data", snapshot["raw_path"]
                )

                if (
                    not raw.is_file()
                    or file_hash(raw) != snapshot["raw_sha256"]
                ):
                    raise ValueError(
                        "Raw archive unavailable for snapshot: "
                        + snapshot["raw_sha256"]
                    )

        with connect(temporary_db) as restored, restored.transaction():
            restored.execute(
                "GRANT USAGE ON SCHEMA serving TO election_dashboard"
            )
            restored.execute(
                "GRANT SELECT ON ALL TABLES IN SCHEMA serving "
                "TO election_dashboard"
            )
            restored.execute("SET LOCAL ROLE election_dashboard")
            restored.execute(
                "SELECT count(*) FROM serving.current_publications"
            ).fetchone()

            can_update = restored.execute(
                "SELECT has_table_privilege(current_user, "
                "'serving.current_publications', 'UPDATE') AS ok"
            ).fetchone()["ok"]

            if can_update:
                raise ValueError(
                    "Restored dashboard role can change the current publication"
                )

        report.update(
            outcome="passed",
            database_inventory=actual,
            raw_archives_verified=len(snapshots),
        )

    except Exception as error:
        report.update(
            outcome="failed",
            error=str(error).replace(
                db["password"], "[redacted]"
            ),
        )

    finally:
        if created:
            try:
                with connect({**db, "dbname": "postgres"}) as admin:
                    admin.execute(
                        sql.SQL("DROP DATABASE {}").format(
                            sql.Identifier(name)
                        )
                    )

                report["temporary_database_removed"] = True

            except Exception as error:
                report["outcome"] = "failed"
                report["cleanup_error"] = str(error).replace(
                    db["password"], "[redacted]"
                )

        report["finished_at"] = utc_now()
        report["report_path"] = str(path)
        atomic_json(path, report)

    return report