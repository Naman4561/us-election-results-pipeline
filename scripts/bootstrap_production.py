"""Initialize production roles and checksum-tracked migrations."""

import hashlib
import os
import re
import sys
from pathlib import Path

from psycopg import sql

from election_pipeline.database import connect, connection_settings


def migration_body(text):
    match = re.fullmatch(
        r"(?:\s|--[^\n]*(?:\n|$))*BEGIN;\s*(.*?)\s*COMMIT;\s*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("Migration must have outer BEGIN/COMMIT statements")
    return match.group(1)


def ensure_login(conn, name, password):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("Invalid database role name")
    if not password:
        raise ValueError("Database role password is missing")

    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s",
        (name,),
    ).fetchone()
    verb = sql.SQL("ALTER ROLE" if exists else "CREATE ROLE")
    conn.execute(
        sql.SQL(
            "{} {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(verb, sql.Identifier(name), sql.Literal(password))
    )


def migrate(conn, root, writer):
    paths = sorted((root / "migrations").glob("*.sql"))
    ledger = conn.execute(
        "SELECT to_regclass('ops.schema_migrations') AS t"
    ).fetchone()["t"]

    if not ledger and conn.execute(
        "SELECT to_regclass('ops.sources') AS t"
    ).fetchone()["t"]:
        raise ValueError(
            "Existing schemas have no production migration ledger; "
            "review before adoption"
        )
    if not paths:
        raise ValueError("No migration files found")

    for path in paths:
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        applied = (
            conn.execute(
                "SELECT sha256 FROM ops.schema_migrations WHERE name=%s",
                (path.name,),
            ).fetchone()
            if ledger
            else None
        )
        if applied:
            if applied["sha256"] != fingerprint:
                raise ValueError(
                    f"Applied migration changed: {path.name}; add a new migration"
                )
            continue

        with conn.transaction():
            conn.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(writer))
            )
            conn.execute(migration_body(path.read_text(encoding="utf-8")))
            if not ledger:
                conn.execute("""
                    CREATE TABLE ops.schema_migrations (
                        name text PRIMARY KEY,
                        sha256 text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                    )
                """)
            conn.execute(
                "INSERT INTO ops.schema_migrations(name, sha256) VALUES (%s, %s)",
                (path.name, fingerprint),
            )
        ledger = True


def main():
    db = connection_settings()
    writer = os.environ["PIPELINE_DB_USER"]
    reader = os.environ["DASHBOARD_DB_USER"]
    passwords = [
        db["password"],
        os.environ["PIPELINE_DB_PASSWORD"],
        os.environ["DASHBOARD_DB_PASSWORD"],
    ]
    if len({db["user"], writer, reader, "election_dashboard"}) != 4:
        raise ValueError("Administrator, writer, reader and group must be distinct")

    try:
        with connect(db) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='election_dashboard'"
            ).fetchone():
                conn.execute("CREATE ROLE election_dashboard NOLOGIN")

            ensure_login(conn, writer, passwords[1])
            ensure_login(conn, reader, passwords[2])

            conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(db["dbname"])
                )
            )
            conn.execute(
                sql.SQL(
                    "GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {} TO {}"
                ).format(sql.Identifier(db["dbname"]), sql.Identifier(writer))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(db["dbname"]), sql.Identifier(reader)
                )
            )
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

            migrate(conn, Path(__file__).resolve().parents[1], writer)

            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA serving "
                    "GRANT SELECT ON TABLES TO election_dashboard"
                ).format(sql.Identifier(writer))
            )
            conn.execute(
                sql.SQL("GRANT election_dashboard TO {}").format(
                    sql.Identifier(reader)
                )
            )
            conn.execute(
                sql.SQL(
                    "ALTER ROLE {} SET default_transaction_read_only=on"
                ).format(sql.Identifier(reader))
            )

        print("Production roles and migrations are ready.")
        return 0
    except Exception as error:
        message = str(error)
        for password in passwords:
            message = message.replace(password, "[redacted]")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())