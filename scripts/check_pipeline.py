"""Run the suite in a newly created disposable PostgreSQL database."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from psycopg import sql

from election_pipeline.database import connect, connection_settings


def main():
    root = Path(__file__).resolve().parents[1]
    db = connection_settings(root / ".env")

    name = "election_acceptance_" + uuid.uuid4().hex[:16] + "_test"

    created = False
    code = 1

    try:
        with connect({**db, "dbname": "postgres"}) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
            created = True

        environment = {
            **os.environ,
            "ELECTION_TEST_DATABASE": name,
        }

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            ],
            cwd=root,
            env=environment,
            check=False,
        )

        code = result.returncode

    except Exception as error:
        print(
            str(error).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )

    finally:
        if created:
            try:
                with connect({**db, "dbname": "postgres"}) as conn:
                    conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

            except Exception as error:
                code = 1
                print(
                    f"Temporary DB {name} needs cleanup: "
                    + str(error).replace(db["password"], "[redacted]"),
                    file=sys.stderr,
                )

    return code


if __name__ == "__main__":
    raise SystemExit(main())
