"""Apply the reviewed SQL migrations in order, each once, pinned by its SHA-256."""

import hashlib
import re
from pathlib import Path

from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]


def migration_body(text):
    match = re.fullmatch(
        r"(?:\s|--[^\n]*(?:\n|$))*BEGIN;\s*(.*?)\s*COMMIT;\s*",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError("Migration must have outer BEGIN/COMMIT statements")
    return match.group(1)


def migrate(conn, root=ROOT, writer=None, through=None):
    """Apply pending migrations as ``writer`` (the owning role); return their names.

    An applied migration whose file changed is refused: add a new migration instead.
    ``through`` stops after that migration (consolidation tests start before 024).
    """
    paths = sorted((Path(root) / "migrations").glob("*.sql"))
    if through:
        paths = [p for p in paths if p.name <= through]
    if not paths:
        raise ValueError("No migration files found")
    ledger = conn.execute("SELECT to_regclass('ops.schema_migrations') AS t").fetchone()["t"]
    if not ledger and conn.execute("SELECT to_regclass('ops.sources') AS t").fetchone()["t"]:
        raise ValueError(
            "Existing schemas have no production migration ledger; review before adoption"
        )
    applied = []
    for path in paths:
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
        recorded = (
            conn.execute(
                "SELECT sha256 FROM ops.schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if ledger
            else None
        )
        if recorded:
            if recorded["sha256"] != fingerprint:
                raise ValueError(f"Applied migration changed: {path.name}; add a new migration")
            continue
        with conn.transaction():
            if writer:
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(writer)))
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
        applied.append(path.name)
    return applied
