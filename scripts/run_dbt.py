"""Run one dbt invocation with project paths and existing PostgreSQL settings."""

import argparse
import os
import re
import sys
from pathlib import Path

from election_pipeline.database import connection_settings


def main():
    root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("debug", "parse", "build", "test"),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=root / ".env",
    )
    parser.add_argument("--schema", default="analytics_dev")

    args, extra = parser.parse_known_args()

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", args.schema):
        parser.error("Schema must be a lowercase identifier of at most 40 characters")

    db = connection_settings(args.env_file)

    os.environ.update(
        {
            "POSTGRES_HOST": str(db["host"]),
            "POSTGRES_PORT": str(db["port"]),
            "POSTGRES_DB": db["dbname"],
            "POSTGRES_USER": db["user"],
            "DBT_ENV_SECRET_POSTGRES_PASSWORD": db["password"],
            "DBT_TARGET_SCHEMA": args.schema,
        }
    )

    from dbt.cli.main import dbtRunner

    result = dbtRunner().invoke(
        [
            args.action,
            "--project-dir",
            str(root / "dbt"),
            "--profiles-dir",
            str(root / "dbt"),
            *extra,
        ]
    )

    if result.exception:
        print(
            str(result.exception).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
