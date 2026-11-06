"""Load verified parser artifacts with transactional COPY and per-release locking."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import traceback
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from election_pipeline.pipeline import collector_code_hash
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, atomic_json, file_hash, utc_now

LOAD_CONTRACT_VERSION = 1


def connection_settings(env_file=Path(".env")):
    values = dotenv_values(env_file, interpolate=False)

    def get(name, default=None):
        return os.environ.get(name, values.get(name, default))

    required = {
        key: get("POSTGRES_" + name)
        for key, name in (
            ("dbname", "DB"),
            ("user", "USER"),
            ("password", "PASSWORD"),
        )
    }

    if not all(required.values()):
        raise ValueError("Set POSTGRES_DB, POSTGRES_USER, and POSTGRES_PASSWORD in .env")

    return {
        **required,
        "host": get("POSTGRES_HOST", "127.0.0.1"),
        "port": int(get("POSTGRES_PORT", "5432")),
        "connect_timeout": 10,
    }


def connect(settings):
    return psycopg.connect(
        **settings,
        autocommit=True,
        row_factory=dict_row,
        options=("-c timezone=UTC -c lock_timeout=15000 -c statement_timeout=120000"),
    )


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a full lowercase SHA-256")

    return value


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"Invalid nonnegative integer: {value!r}")

    return int(value)


def timestamp(value):
    if value is None:
        return None

    result = datetime.fromisoformat(value)

    if result.tzinfo is None:
        raise ValueError("Operational timestamps must include a timezone")

    return result


def verify(source, settings, store, directory):
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))

    if not isinstance(report, dict):
        raise ValueError("Parser report must be a JSON object")

    adapter = state_adapter(source.state)
    contract = adapter.contract(source.dataset)
    if source.format != contract.format:
        raise ValueError("Unsupported source format for this dataset")
    table = adapter.schema + "_" + contract.table
    filename, fields, files = contract.filename, contract.fields, contract.files

    expected = {
        "source_id": source.source_id,
        "dataset": source.dataset,
        "election_date": source.election_date,
        "source_url": source.url,
        "parser_contract_version": 1,
        "outcome": "parsed",
        "internal_reconciliation": "passed",
    }

    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("Parser report identity, contract, or validation status does not match")

    comparison = report.get("reference_comparison")

    if not isinstance(comparison, dict):
        raise ValueError("Missing reference-comparison status")

    status = comparison.get("status")
    allowed = (
        {"matched", "not_provided"} if source.dataset == "election_results" else {"not_applicable"}
    )

    if status not in allowed:
        raise ValueError("Parser reference comparison is invalid or mismatched")

    identity = report["identity"]

    if not isinstance(identity, dict):
        raise ValueError("Parser identity must be a JSON object")

    if set(identity) != {
        "raw_sha256",
        "source_signature",
        "code_sha256",
        "reference_sha256",
    }:
        raise ValueError("Unsupported parser identity fields")

    for key, value in identity.items():
        if key != "reference_sha256" or value is not None:
            digest(value)

    if identity["source_signature"] != source.signature(settings):
        raise ValueError("Source configuration changed; parse this source again")

    raw = store.archive_path(identity["raw_sha256"])

    if report["raw_archive"] != store.relative(raw) or file_hash(raw) != identity["raw_sha256"]:
        raise ValueError("Raw archive path or SHA-256 mismatch")

    hashes = report["output_sha256"]

    if not isinstance(hashes, dict):
        raise ValueError("Output hashes must be a JSON object")

    if set(hashes) != set(files):
        raise ValueError("Unexpected parser output file set")

    for name, expected_hash in hashes.items():
        if file_hash(directory / name) != digest(expected_hash):
            raise ValueError(f"Parsed output SHA-256 mismatch: {name}")

    if (
        type(report["output_rows"]) is not int
        or not 0 < report["output_rows"] <= settings.max_csv_rows
    ):
        raise ValueError("Invalid expected row count")

    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    return report, raw, fingerprint, table, filename, fields


def register_source(conn, source):
    row = conn.execute(
        """
        INSERT INTO ops.sources
            (source_id,state,election_date,dataset,source_url,format)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT(source_id) DO UPDATE SET source_url=EXCLUDED.source_url
        WHERE (sources.state,sources.election_date,sources.dataset,sources.format) =
              (EXCLUDED.state,EXCLUDED.election_date,EXCLUDED.dataset,EXCLUDED.format)
        RETURNING source_id
        """,
        (
            source.source_id,
            source.state,
            source.election_date,
            source.dataset,
            source.url,
            source.format,
        ),
    ).fetchone()

    if row is None:
        raise ValueError("A source ID cannot change its state, election, dataset, or format")


def register_snapshot(conn, store, sha):
    path = store.archive_path(digest(sha))
    size = path.stat().st_size if path.is_file() else None

    row = conn.execute(
        """
        INSERT INTO ops.snapshots(source_id,raw_sha256,raw_path,raw_bytes)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(source_id,raw_sha256) DO UPDATE
            SET raw_bytes=COALESCE(snapshots.raw_bytes,EXCLUDED.raw_bytes)
        RETURNING snapshot_id
        """,
        (store.source.source_id, sha, store.relative(path), size),
    ).fetchone()

    return row["snapshot_id"]


def import_fetches(conn, store):
    """Import fetch records not yet finished in the database.

    A record's file name is its fetch ID, and a finished row is never updated again (see
    the upsert below), so files already imported as finished are skipped unread. Each
    load then costs one directory listing instead of rereading every attempt ever made.
    """
    count = 0
    folder = store.root / "metadata" / store.source.source_id
    finished = {
        row["fetch_id"].hex
        for row in conn.execute(
            "SELECT fetch_id FROM ops.fetch_attempts "
            "WHERE source_id=%s AND finished_at IS NOT NULL",
            (store.source.source_id,),
        )
    }

    for path in sorted(folder.glob("*/*.json")):
        if path.stem in finished:
            continue
        record = json.loads(path.read_text(encoding="utf-8"))

        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise ValueError(f"Unsupported fetch metadata contract: {path.name}")

        if (
            record["source_id"],
            record["state"],
            record["election_date"],
        ) != (
            store.source.source_id,
            store.source.state,
            store.source.election_date,
        ):
            raise ValueError(f"Fetch metadata source mismatch: {path.name}")

        snapshot = None

        if record["outcome"] in {"downloaded", "unchanged", "manual_imported"}:
            snapshot = register_snapshot(conn, store, record["sha256"])

        modified = record.get("response_headers", {}).get("last-modified")

        try:
            modified = parsedate_to_datetime(modified) if modified else None

            if modified is not None and modified.tzinfo is None:
                modified = None

        except (ValueError, TypeError, OverflowError):
            modified = None

        if record["outcome"] == "manual_imported":
            # This is a report timestamp extracted from the validated document,
            # not an invented HTTP Last-Modified response header.
            modified = timestamp(record["source_generated_at"])

        conn.execute(
            """
            INSERT INTO ops.fetch_attempts
                (fetch_id,run_id,source_id,snapshot_id,started_at,finished_at,
                 http_status,outcome,source_modified_at,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(fetch_id) DO UPDATE SET
                snapshot_id=EXCLUDED.snapshot_id,
                finished_at=EXCLUDED.finished_at,
                http_status=EXCLUDED.http_status,
                outcome=EXCLUDED.outcome,
                source_modified_at=EXCLUDED.source_modified_at,
                metadata=EXCLUDED.metadata
            WHERE fetch_attempts.finished_at IS NULL
            """,
            (
                uuid.UUID(record["fetch_id"]),
                uuid.UUID(record["run_id"]),
                store.source.source_id,
                snapshot,
                timestamp(record["started_at"]),
                timestamp(record.get("finished_at")),
                record["http_status"],
                record["outcome"],
                modified,
                Jsonb(record),
            ),
        )

        count += 1

    return count


def converted_rows(path, fields, source, release, expected_rows):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)

        if reader.fieldnames != list(fields):
            raise ValueError("Unexpected detailed CSV header")

        for position, row in enumerate(reader, 1):
            if position > expected_rows:
                raise ValueError("Detailed CSV exceeds its expected row count")

            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed detailed CSV record")

            bad_fields = [name for name, value in row.items() if "\x00" in value]
            if bad_fields:
                raise ValueError(
                    f"Detailed CSV row {position}: NUL bytes in {', '.join(bad_fields)}; "
                    "reparse the original source with the corrected parser"
                )

            if row["state"] != source.state or row["election_date"] != source.election_date:
                raise ValueError("Detailed CSV election/state mismatch")

            yield state_adapter(source.state).parser.database_row(row, source, release)


def load_parsed(source, settings, data_dir, parsed_dir, db):
    store = ArchiveStore(data_dir, source)
    directory = parsed_dir.resolve()
    attempt = uuid.uuid4()
    release = None
    conn = None

    result = {
        "source_id": source.source_id,
        "load_attempt_id": str(attempt),
    }

    try:
        conn = connect(db)

        conn.execute(
            """
            INSERT INTO ops.load_attempts
                (load_attempt_id,input_report_path,loader_code_sha256)
            VALUES (%s,%s,%s)
            """,
            (
                attempt,
                str(directory / "report.json"),
                collector_code_hash(),
            ),
        )

        report_hash = file_hash(directory / "report.json")

        report, raw, fingerprint, table, filename, fields = verify(
            source, settings, store, directory
        )

        with conn.transaction():
            register_source(conn, source)
            fetches = import_fetches(conn, store)

            snapshot = register_snapshot(conn, store, report["identity"]["raw_sha256"])

            conn.execute(
                """
                INSERT INTO ops.processing_releases
                    (snapshot_id,parser_fingerprint,load_contract_version,
                     report_path,parser_report,expected_rows)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                """,
                (
                    snapshot,
                    fingerprint,
                    LOAD_CONTRACT_VERSION,
                    str(directory / "report.json"),
                    Jsonb(report),
                    report["output_rows"],
                ),
            )

            record = conn.execute(
                """
                SELECT *
                FROM ops.processing_releases
                WHERE snapshot_id=%s
                  AND parser_fingerprint=%s
                  AND load_contract_version=%s
                """,
                (snapshot, fingerprint, LOAD_CONTRACT_VERSION),
            ).fetchone()

            release = record["release_id"]

            if (
                record["parser_report"]["output_sha256"] != report["output_sha256"]
                or record["expected_rows"] != report["output_rows"]
            ):
                raise ValueError("Same release identity produced different output; investigate")

        conn.execute(
            """
            UPDATE ops.load_attempts
            SET release_id=%s
            WHERE load_attempt_id=%s
            """,
            (release, attempt),
        )

        with conn.transaction():
            record = conn.execute(
                """
                SELECT *
                FROM ops.processing_releases
                WHERE release_id=%s
                FOR UPDATE
                """,
                (release,),
            ).fetchone()

            adapter = state_adapter(source.state)
            contract = adapter.contract(source.dataset)
            skipped = record["loaded_at"] is not None
            rows_to_load = [(contract.table, filename, fields, report["output_rows"])]
            for relation, artifact, columns, count_key in contract.auxiliary:
                expected_count = report.get(count_key)
                if (
                    type(expected_count) is not int
                    or not 0 < expected_count <= settings.max_csv_rows
                ):
                    raise ValueError("Invalid auxiliary row count")
                rows_to_load.append((relation, artifact, columns, expected_count))
            for relation, artifact, columns, expected_count in rows_to_load:
                target = sql.Identifier(adapter.schema, relation)
                count_sql = sql.SQL("SELECT count(*) AS n FROM {} WHERE release_id=%s").format(
                    target
                )
                count = conn.execute(count_sql, (release,)).fetchone()["n"]
                if skipped:
                    if count != expected_count:
                        raise ValueError(
                            "Previously loaded release has an unexpected database row count"
                        )
                else:
                    if count:
                        raise ValueError("Unfinished release already contains rows")
                    copied = ["release_id", "source_line"] + [
                        k for k in columns if k not in {"source_line", "state", "election_date"}
                    ]
                    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
                        target, sql.SQL(",").join(map(sql.Identifier, copied))
                    )
                    with conn.cursor().copy(copy_sql) as copy:
                        for row in converted_rows(
                            directory / artifact, columns, source, release, expected_count
                        ):
                            copy.write_row(row)
                    count = conn.execute(count_sql, (release,)).fetchone()["n"]
                    if count != expected_count:
                        raise ValueError("Loaded row count does not match parser report")
            count = report["output_rows"]
            if not skipped:
                conn.execute(
                    "UPDATE ops.processing_releases SET loaded_rows=%s,loaded_at=clock_timestamp() "
                    "WHERE release_id=%s",
                    (count, release),
                )

            if file_hash(directory / "report.json") != report_hash:
                raise ValueError("Parser report changed during loading")

            verify(source, settings, store, directory)

            outcome = "skipped" if skipped else "succeeded"
            rows_loaded = 0 if skipped else count

            conn.execute(
                """
                UPDATE ops.load_attempts
                SET outcome=%s,
                    rows_loaded=%s,
                    finished_at=clock_timestamp()
                WHERE load_attempt_id=%s
                """,
                (outcome, rows_loaded, attempt),
            )

        return {
            **result,
            "outcome": outcome,
            "release_id": release,
            "rows_loaded": rows_loaded,
            "fetch_records_seen": fetches,
        }

    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        csv.Error,
        psycopg.Error,
    ) as error:
        secret = db.get("password", "") or "\0"
        message = str(error).replace(secret, "[redacted]")

        result.update(
            outcome="failed",
            release_id=release,
            error=message,
            finished_at=utc_now(),
        )

        if conn is not None and not conn.closed:
            try:
                conn.execute(
                    """
                    UPDATE ops.load_attempts
                    SET outcome='failed',
                        error_message=%s,
                        finished_at=clock_timestamp()
                    WHERE load_attempt_id=%s
                    """,
                    (message, attempt),
                )
            except psycopg.Error:
                pass

        path = store.root / "reports" / "loading" / source.source_id / f"{attempt.hex}.json"

        atomic_json(
            path,
            {
                **result,
                "traceback": traceback.format_exc().replace(secret, "[redacted]"),
            },
        )

        return {**result, "report_path": str(path)}

    finally:
        if conn is not None:
            conn.close()
