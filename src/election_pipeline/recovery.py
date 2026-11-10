"""Docker PostgreSQL backups and isolated restoration checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from psycopg import sql

from election_pipeline.database import connect
from election_pipeline.settings import load_config
from election_pipeline.storage import (
    ArchiveStore,
    CollectionLocked,
    atomic_json,
    file_hash,
    utc_now,
)

# Per-state result schemas from before migration 024. Their rows were copied into the
# shared tables at cutover; they stay, unused, as that copy's original and are backed up.
CONSOLIDATED_SCHEMAS = (
    "ak", "ca", "co", "fl", "ga", "hi", "ia", "id", "ks", "ky", "md", "mn",
    "ne", "nm", "oh", "or", "sc", "sd", "tx", "va", "wa", "wv",
)  # fmt: skip
SCHEMAS = ("ops", "landing", "serving", "ms", "nc", *CONSOLIDATED_SCHEMAS)

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
# Files rewritten in place, copied while jobs are held. Every other file is written once.
REWRITTEN_DIRECTORIES = ("state", "accuracy", "reports", "recovery_checks")
# prune_data.py may remove old files here while the unlocked copy runs.
PRUNABLE_DIRECTORIES = ("runs", "parsed", "publications")

# The backup waits this long for running jobs to finish before giving up.
QUIESCE_SECONDS = 600
QUIESCE_RETRY_SECONDS = 5

# Files written once are not copied into every bundle. Each backup writes the ones new
# since the previous backup into packs (zips, one for raw archives and one for the rest)
# that are uploaded once and kept; its manifest maps every such file to its pack.
MANIFEST_VERSIONS = (1, 2)
PACK_KINDS = ("raw", "files")
# Already compressed; stored in a pack as they are.
STORED_SUFFIXES = {".zip", ".gz", ".xlsx", ".png", ".jpg", ".jpeg", ".pdf"}


def pack_kind(relative):
    return "raw" if PurePosixPath(relative).parts[0] == "raw" else "files"


def pack_name(identity, kind):
    return f"{identity}-{kind}.zip"


def safe_child(root, relative):
    """Reject absolute paths, traversal and symlinks."""
    parts = PurePosixPath(relative)

    if (
        not parts.parts
        or parts.is_absolute()
        or any(part in ("..", ".") or "\\" in part or ":" in part for part in parts.parts)
    ):
        raise ValueError("Unsafe backup path")

    result = Path(root).joinpath(*parts.parts)

    if any(path.is_symlink() for path in [result, *result.parents]):
        raise ValueError("Backup paths must not be symlinks")

    if not result.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Backup path escapes its directory")

    return result


def inventory(conn, schemas=SCHEMAS):
    """Fingerprint every business/audit table in one DB transaction."""
    # One sorted read of the shared landing tables (millions of rows) takes minutes, past
    # the connection's statement limit; this lifts it for the caller's read-only
    # transaction only.
    conn.execute("SET LOCAL statement_timeout = 0")
    tables = conn.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(%s)
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
        """,
        (list(schemas),),
    ).fetchall()

    found = {row["table_schema"] for row in tables}

    if not {"ops", "landing", "serving"}.issubset(found):
        raise ValueError("Apply the ingestion and publication migrations first")

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
    compose_args=(),
):
    """Stream binary files without a shell or password arguments."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", db["dbname"]):
        raise ValueError("Backup expects a simple PostgreSQL database name")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service):
        raise ValueError("Invalid Compose service name")

    command = [
        "docker",
        "compose",
        *compose_args,
        "exec",
        "-T",
        service,
        program,
    ]

    needs_connection = program == "pg_dump" or (
        program == "pg_restore" and "--list" not in arguments
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

    message = result.stderr.decode("utf-8", errors="replace").replace(db["password"], "[redacted]")

    if result.returncode:
        raise ValueError(f"{program} failed: {message[-4000:]}")

    return message.strip()


def files(source):
    """The files under ``source`` a backup keeps, relative to it."""
    if not source.exists():
        return []

    result = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)

        if (
            any(part.startswith(".") for part in relative.parts)
            or path.suffix in (".part", ".tmp", ".pyc")
            or "__pycache__" in relative.parts
        ):
            continue

        if path.is_symlink():
            raise ValueError(f"Refusing symlink in backup: {relative}")

        if path.is_file():
            result.append(relative)
    return result


def copy_files(source, destination, relatives=None, *, missing_ok=False):
    for relative in files(source) if relatives is None else relatives:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source / relative, target)
        except FileNotFoundError:
            if not missing_ok:
                raise


def pack_files(data_dir, written_once, packs_dir, identity, previous=None):
    """Map every write-once file to a pack, writing the new ones into this backup's packs.

    A file the previous manifest lists with the same size and modification time is
    already in one of its packs and is not read again. Returns the manifest's ``data``
    ({data-relative path: sha256, bytes, mtime_ns, pack}) and ``packs`` (every pack
    those entries name, with its size and hash).
    """
    known, known_packs = {}, {}
    if previous is not None and previous.get("schema_version") == 2:
        known, known_packs = previous["data"], previous["packs"]
    data, writers = {}, {}
    with ExitStack() as open_packs:
        for directory, listed in written_once.items():
            for relative in listed:
                key = PurePosixPath(directory, *relative.parts).as_posix()
                path = data_dir / directory / relative
                try:
                    status = path.stat()
                    entry = known.get(key)
                    if (
                        entry is not None
                        and entry["bytes"] == status.st_size
                        and entry["mtime_ns"] == status.st_mtime_ns
                    ):
                        data[key] = entry
                        continue
                    digest = file_hash(path)
                except FileNotFoundError:
                    if directory in PRUNABLE_DIRECTORIES:
                        continue
                    raise
                kind = pack_kind(key)
                if kind not in writers:
                    packs_dir.mkdir(parents=True, exist_ok=True)
                    writers[kind] = open_packs.enter_context(
                        zipfile.ZipFile(packs_dir / pack_name(identity, kind), "w")
                    )
                writers[kind].write(
                    path,
                    arcname=key,
                    compress_type=(
                        zipfile.ZIP_STORED
                        if path.suffix.lower() in STORED_SUFFIXES
                        else zipfile.ZIP_DEFLATED
                    ),
                    compresslevel=1,
                )
                data[key] = {
                    "sha256": digest,
                    "bytes": status.st_size,
                    "mtime_ns": status.st_mtime_ns,
                    "pack": pack_name(identity, kind),
                }
    packs = {}
    for name in sorted({entry["pack"] for entry in data.values()}):
        path = packs_dir / name
        if name in known_packs:
            packs[name] = known_packs[name]
        elif path.is_file():
            packs[name] = {
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
                "created_at": utc_now(),
            }
        else:
            raise ValueError("A file's pack is missing from the previous backup: " + name)
    return data, packs


def unpack_pack(manifest, pack, data_dir, wanted=None):
    """Extract a downloaded pack's files into ``data_dir``, verifying each against the manifest.

    Only the files the manifest assigns to this pack (and in ``wanted``, if given) are
    extracted. Returns their data-relative paths.
    """
    pack = Path(pack)
    expected = manifest["packs"].get(pack.name)
    if (
        expected is None
        or pack.stat().st_size != expected["bytes"]
        or file_hash(pack) != expected["sha256"]
    ):
        raise ValueError("Pack does not match the backup manifest: " + pack.name)
    extracted = []
    with zipfile.ZipFile(pack) as zipped:
        for member in zipped.infolist():
            entry = manifest["data"].get(member.filename)
            if entry is None or entry["pack"] != pack.name:
                continue  # a file pruned since, or kept in a newer pack
            if wanted is not None and member.filename not in wanted:
                continue
            target = safe_child(data_dir, member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if target.stat().st_size != entry["bytes"] or file_hash(target) != entry["sha256"]:
                raise ValueError("Packed file does not match the backup manifest: " + target.name)
            extracted.append(member.filename)
    return extracted


@contextmanager
def quiesced(data_dir, sources, conn, wait=QUIESCE_SECONDS):
    """Hold every source's locks and the exclusive maintenance lock.

    All locks are tried together and released together while any job still runs, so the
    backup never holds some sources while waiting for another; jobs that start meanwhile
    run normally. Gives up after ``wait`` seconds.
    """
    deadline = time.monotonic() + wait
    while True:
        with ExitStack() as held:
            try:
                for source in sorted(sources.values(), key=lambda item: item.source_id):
                    store = ArchiveStore(data_dir, source)
                    held.enter_context(store.lock(stage="pipeline"))
                    held.enter_context(store.lock())
            except CollectionLocked as error:
                busy = f"source {error}"
            else:
                if conn.execute("SELECT pg_try_advisory_lock(20261103, 1) AS ok").fetchone()["ok"]:
                    yield
                    # On an error the connection closes and releases it instead.
                    conn.execute("SELECT pg_advisory_unlock(20261103, 1)")
                    return
                busy = "a publication or restore"
        if time.monotonic() >= deadline:
            raise ValueError(f"Jobs still running after {wait} s ({busy}); retry backup later")
        time.sleep(QUIESCE_RETRY_SECONDS)


def backup(
    root,
    data_dir,
    config,
    db,
    destination,
    service,
    *,
    compose_args=(),
    catalog=None,
    previous=None,
):
    """Capture files and a dump using the inventory's DB snapshot.

    Jobs are held only while the snapshot is exported, the rewritten files are copied
    and the write-once files are listed; packing, the inventory and the dump run after.
    ``previous`` is the last uploaded backup's manifest: the files it already packed are
    referenced, not packed again.
    """
    root = Path(root).resolve()
    data_dir = Path(data_dir).resolve()
    destination = Path(destination).resolve()

    if destination.is_relative_to(data_dir) or data_dir.is_relative_to(destination):
        raise ValueError("Choose a backup destination outside the live data directory")

    _, sources = load_config(config)
    database_schemas = SCHEMAS
    if catalog is not None:
        from election_pipeline.deployment import catalog as registry

        registry.validate(catalog)

    identity = "backup_" + uuid.uuid4().hex

    # The manifest written last marks this bundle as complete.
    temporary = destination / identity
    temporary.mkdir(parents=True, exist_ok=False)

    try:
        manifest = {
            "schema_version": 2,
            "started_at": utc_now(),
            "database": db["dbname"],
        }

        with ExitStack() as stack:
            conn = stack.enter_context(connect(db))
            written_once = {}

            with quiesced(data_dir, sources, conn):
                stack.enter_context(conn.transaction())
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

                snapshot = conn.execute("SELECT pg_export_snapshot() AS id").fetchone()["id"]

                for directory in DATA_DIRECTORIES:
                    listed = files(data_dir / directory)
                    rewritten = directory in REWRITTEN_DIRECTORIES
                    in_place = [r for r in listed if rewritten or r.name == "latest.json"]
                    copy_files(data_dir / directory, temporary / "data" / directory, in_place)
                    written_once[directory] = [
                        r for r in listed if not rewritten and r.name != "latest.json"
                    ]

            manifest["data"], manifest["packs"] = pack_files(
                data_dir, written_once, temporary / "packs", identity, previous
            )
            previous = None  # the old manifest is large; it is not needed past this point

            manifest["database_inventory"] = inventory(conn, database_schemas)
            manifest["database_schemas"] = [
                row["schema_name"]
                for row in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name=ANY(%s) "
                    "ORDER BY schema_name",
                    (list(database_schemas),),
                ).fetchall()
            ]

            snapshots = conn.execute("SELECT raw_path, raw_sha256 FROM ops.snapshots").fetchall()

            for row in snapshots:
                entry = manifest["data"].get(PurePosixPath(row["raw_path"]).as_posix())

                if entry is None or entry["sha256"] != row["raw_sha256"]:
                    raise ValueError(
                        "Missing/corrupt raw archive for snapshot: " + row["raw_sha256"]
                    )

            shutil.copy2(config, temporary / "sources.toml")

            if catalog is not None:
                atomic_json(temporary / "deployment-catalog.json", catalog)
                # Preserve the current release and the rollback one.
                releases = {}
                for entry in (catalog["release"], catalog.get("previous_release")):
                    if not entry or entry["revision"] in releases:
                        continue
                    revision, release = entry["revision"], Path(entry["release_path"])
                    actual = subprocess.check_output(
                        ["git", "-C", str(release), "rev-parse", "HEAD"], text=True
                    ).strip()
                    dirty = subprocess.check_output(
                        ["git", "-C", str(release), "status", "--porcelain"], text=True
                    ).strip()
                    if actual != revision or dirty:
                        raise ValueError("Release checkout changed")
                    releases[revision] = str(release)
                    target = temporary / "releases" / revision
                    for directory in (
                        "src",
                        "scripts",
                        "migrations",
                        "config",
                        "dbt/models",
                        "dbt/macros",
                        "dbt/tests",
                        "deploy",
                    ):
                        copy_files(release / directory, target / directory)
                    for pattern in (
                        "pyproject.toml",
                        "requirements-dev.txt",
                        "Dockerfile",
                        "compose.production.yml",
                        "dbt/*.yml",
                    ):
                        for file in release.glob(pattern):
                            destination_file = target / file.relative_to(release)
                            destination_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(file, destination_file)
                manifest["collector_releases"] = releases
                for dashboard in (
                    catalog.get("dashboard", {}),
                    catalog.get("previous_dashboard", {}),
                ):
                    if dashboard.get("coverage_dir"):
                        coverage = Path(dashboard["coverage_dir"]) / "catalog.json"
                        target = (
                            temporary / "dashboard-coverage" / dashboard["name"] / "catalog.json"
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(coverage, target)

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
                    shutil.copy2(path, temporary / "code/dbt" / path.name)

            for filename in ("pyproject.toml", "requirements-dev.txt"):
                if (root / filename).is_file():
                    shutil.copy2(root / filename, temporary / filename)

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
                        *["--schema=" + schema for schema in manifest["database_schemas"]],
                    ],
                    stdout=handle,
                    compose_args=compose_args,
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
                    compose_args=compose_args,
                )

        manifest["finished_at"] = utc_now()
        # The packs are uploaded on their own; the manifest records them under "packs".
        manifest["files"] = {
            path.relative_to(temporary).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
            }
            for path in sorted(temporary.rglob("*"))
            if path.is_file() and path.relative_to(temporary).parts[0] != "packs"
        }

        atomic_json(temporary / "manifest.json", manifest)

        return {
            "outcome": "backed_up",
            "backup_dir": str(temporary),
            "files": len(manifest["files"]),
            "data_files": len(manifest["data"]),
            "new_packs": sorted(p.name for p in (temporary / "packs").glob("*.zip")),
        }
    except BaseException:
        # An unfinished bundle has no manifest and cannot be restored; left behind, each
        # failed run would keep a full uncompressed copy of the data on the disk.
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_bundle(directory):
    directory = Path(directory).resolve()

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    if manifest.get("schema_version") not in MANIFEST_VERSIONS or "database.dump" not in (
        manifest.get("files", {})
    ):
        raise ValueError("Unsupported/incomplete backup manifest")
    if manifest["schema_version"] == 2 and not (
        isinstance(manifest.get("data"), dict) and isinstance(manifest.get("packs"), dict)
    ):
        raise ValueError("Unsupported/incomplete backup manifest")

    for relative, expected in manifest["files"].items():
        path = safe_child(directory, relative)

        if path.stat().st_size != expected["bytes"] or file_hash(path) != expected["sha256"]:
            raise ValueError(f"Backup checksum failed: {relative}")

    return manifest


def check_restore(
    root,
    db,
    directory,
    service,
    *,
    compose_args=(),
    writer_role=None,
    reader_role="election_dashboard",
    report_dir=None,
):
    """Restore into a new temporary database, verify it, then remove it."""
    directory = Path(directory).resolve()
    manifest = verify_bundle(directory)

    for role in (writer_role, reader_role):
        if role is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role):
            raise ValueError("Invalid restore role name")

    name = "election_restore_" + uuid.uuid4().hex[:16] + "_test"
    temporary_db = {**db, "dbname": name}
    report = {
        "outcome": "running",
        "started_at": utc_now(),
        "temporary_database": name,
        "backup_dir": str(directory),
    }
    path = Path(report_dir or Path(root) / "data/recovery_checks") / f"{name}.json"
    created = False

    try:
        with connect({**db, "dbname": "postgres"}) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
            created = True

        if writer_role:
            with connect(temporary_db) as admin:
                admin.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
                )
                admin.execute(
                    sql.SQL("GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {} TO {}").format(
                        sql.Identifier(name),
                        sql.Identifier(writer_role),
                    )
                )
                admin.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(name),
                        sql.Identifier(reader_role),
                    )
                )

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
                    *(["--role=" + writer_role] if writer_role else []),
                ],
                stdin=handle,
                compose_args=compose_args,
            )

        if writer_role:
            with (
                connect(temporary_db) as restored,
                restored.transaction(force_rollback=True),
            ):
                restored.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(writer_role)))
                for table in manifest["database_inventory"]:
                    schema, relation = table.split(".", 1)
                    owner = restored.execute(
                        "SELECT pg_get_userbyid(c.relowner) AS owner "
                        "FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=%s AND c.relname=%s",
                        (schema, relation),
                    ).fetchone()
                    if owner["owner"] != writer_role:
                        raise ValueError("Restored table ownership does not match the writer")

                restored.execute("UPDATE ops.sources SET source_id=source_id WHERE false")
                restored.execute(
                    "UPDATE serving.current_publications "
                    "SET publication_id=publication_id WHERE false"
                )

        with connect(temporary_db) as restored, restored.transaction():
            restored.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            actual = inventory(restored, manifest.get("database_schemas", SCHEMAS))
            if actual != manifest["database_inventory"]:
                raise ValueError("Restored table counts/hashes do not match the backup snapshot")

            snapshots = restored.execute(
                "SELECT raw_path, raw_sha256 FROM ops.snapshots"
            ).fetchall()

            for snapshot in snapshots:
                raw = safe_child(directory / "data", snapshot["raw_path"])
                if not raw.is_file() or file_hash(raw) != snapshot["raw_sha256"]:
                    raise ValueError(
                        "Raw archive unavailable for snapshot: " + snapshot["raw_sha256"]
                    )
            restored_publications = {
                row["source_id"]: str(row["publication_id"])
                for row in restored.execute(
                    "SELECT source_id, publication_id FROM serving.current_publications"
                ).fetchall()
            }

        with connect(temporary_db) as restored, restored.transaction():
            restored.execute("GRANT USAGE ON SCHEMA serving TO election_dashboard")
            restored.execute("GRANT SELECT ON ALL TABLES IN SCHEMA serving TO election_dashboard")
            for schema in manifest.get("database_schemas", SCHEMAS):
                if not re.fullmatch(r"[a-z]{2}", schema):
                    continue
                for table in (
                    "county_candidate_results",
                    "contest_candidate_results",
                    "county_absentee_demographics",
                    "state_absentee_demographics",
                    "county_feed_status",
                ):
                    relation = schema + "." + table
                    if relation in manifest["database_inventory"]:
                        restored.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA {} TO election_dashboard").format(
                                sql.Identifier(schema)
                            )
                        )
                        restored.execute(
                            sql.SQL("GRANT SELECT ON {} TO election_dashboard").format(
                                sql.Identifier(schema, table)
                            )
                        )
            if writer_role:
                restored.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA serving "
                        "GRANT SELECT ON TABLES TO election_dashboard"
                    ).format(sql.Identifier(writer_role))
                )

            restored.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(reader_role)).as_string()
            )
            restored.execute("SELECT count(*) FROM serving.current_publications").fetchone()
            published_tables = {
                "county_candidate_results",
                "contest_candidate_results",
                "county_absentee_demographics",
                "state_absentee_demographics",
                "county_feed_status",
            }
            for relation in manifest["database_inventory"]:
                schema, table = relation.split(".", 1)
                if not re.fullmatch(r"[a-z]{2}", schema):
                    continue
                if table in published_tables:
                    restored.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
                    ).fetchone()
                    if restored.execute(
                        "SELECT has_table_privilege(current_user, %s, 'UPDATE') AS ok", (relation,)
                    ).fetchone()["ok"]:
                        raise ValueError("Restored dashboard can change state publications")
                elif restored.execute(
                    "SELECT has_table_privilege(current_user, %s, 'SELECT') AS ok", (relation,)
                ).fetchone()["ok"]:
                    raise ValueError("Restored dashboard can read state ingestion tables")

            if restored.execute(
                "SELECT has_table_privilege(current_user, "
                "'serving.current_publications', 'UPDATE') AS ok"
            ).fetchone()["ok"]:
                raise ValueError("Restored dashboard role can change the current publication")

            if restored.execute(
                "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
            ).fetchone()["ok"]:
                raise ValueError("Restored dashboard role can access landing data")

        report.update(
            outcome="passed",
            database_inventory=actual,
            raw_archives_verified=len(snapshots),
            current_publications=restored_publications,
            writer_permissions_verified=bool(writer_role),
            reader_permissions_verified=True,
        )

    except Exception as error:
        report.update(
            outcome="failed",
            error=str(error).replace(db["password"], "[redacted]"),
        )

    finally:
        if created:
            try:
                with connect({**db, "dbname": "postgres"}) as admin:
                    admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                report["temporary_database_removed"] = True
            except Exception as error:
                report["outcome"] = "failed"
                report["cleanup_error"] = str(error).replace(db["password"], "[redacted]")

        report["finished_at"] = utc_now()
        report["report_path"] = str(path)
        atomic_json(path, report)

    return report
