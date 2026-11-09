"""Initialize production roles and checksum-tracked migrations."""

import os
import re
import sys

from psycopg import sql

from election_pipeline.database import connect, connection_settings
from election_pipeline.migrations import migrate


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
                sql.SQL("GRANT CONNECT, CREATE, TEMPORARY ON DATABASE {} TO {}").format(
                    sql.Identifier(db["dbname"]), sql.Identifier(writer)
                )
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(db["dbname"]), sql.Identifier(reader)
                )
            )
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

            migrate(conn, writer=writer)

            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA serving "
                    "GRANT SELECT ON TABLES TO election_dashboard"
                ).format(sql.Identifier(writer))
            )
            conn.execute(sql.SQL("GRANT election_dashboard TO {}").format(sql.Identifier(reader)))
            conn.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(
                    sql.Identifier(reader)
                )
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
