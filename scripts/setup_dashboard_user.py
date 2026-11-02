"""Create a dedicated local reader and save its generated password to .env.dashboard."""

import os
import secrets
from pathlib import Path

from psycopg import sql

from election_pipeline.database import connect, connection_settings


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / ".env.dashboard"
    if target.exists():
        raise ValueError(".env.dashboard already exists; keep your existing reader configuration")
    settings = connection_settings(root / ".env")
    role, password = "election_dashboard_app", secrets.token_urlsafe(32)
    created_file = False
    try:
        with connect(settings) as conn, conn.transaction():
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise ValueError(
                    "Reader role already exists; configure .env.dashboard with its credentials"
                )
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            conn.execute(sql.SQL("GRANT election_dashboard TO {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(
                    sql.Identifier(role)
                )
            )
            content = {
                "DASHBOARD_POSTGRES_HOST": settings["host"],
                "DASHBOARD_POSTGRES_PORT": settings["port"],
                "DASHBOARD_POSTGRES_DB": settings["dbname"],
                "DASHBOARD_POSTGRES_USER": role,
                "DASHBOARD_POSTGRES_PASSWORD": password,
            }
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created_file = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write("".join(f"{key}={value}\n" for key, value in content.items()))
    except Exception:
        if created_file:
            target.unlink(missing_ok=True)
        raise
    print("Created the read-only dashboard login and .env.dashboard. Password was not printed.")


if __name__ == "__main__":
    main()
