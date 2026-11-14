"""The local analysis database: a PostgreSQL container on this computer only.

Its settings and password live in ``~/election-analysis/.env`` (``ANALYSIS_HOME``),
outside the repository, created on first use. The port listens on 127.0.0.1 only.
"""

import os
import secrets
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values

HOME = Path(os.environ.get("ANALYSIS_HOME", Path.home() / "election-analysis"))
CONTAINER = "election-analysis-db"
VOLUME = "election-analysis-pgdata"
# Production runs PostgreSQL 17; a dump restores into the same major version.
IMAGE = "postgres:17"
DATABASE = "elections"


def settings():
    """{host, port, dbname, user, password}, creating the settings file once."""
    HOME.mkdir(parents=True, exist_ok=True)
    path = HOME / ".env"
    if not path.exists():
        path.write_text(
            "ANALYSIS_PORT=55432\nANALYSIS_USER=analyst\n"
            f"ANALYSIS_PASSWORD={secrets.token_urlsafe(24)}\n",
            encoding="utf-8",
        )
    values = dotenv_values(path)
    return {
        "host": "127.0.0.1",
        "port": int(values["ANALYSIS_PORT"]),
        "dbname": DATABASE,
        "user": values["ANALYSIS_USER"],
        "password": values["ANALYSIS_PASSWORD"],
    }


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, **kwargs)


def start(db=None):
    """Start the container (creating it once), and wait until it accepts connections."""
    db = db or settings()
    found = docker("ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.State}}")
    state = found.stdout.strip()
    if not state:
        docker(
            "run", "--detach", "--name", CONTAINER,
            "--publish", f"127.0.0.1:{db['port']}:5432",
            "--volume", f"{VOLUME}:/var/lib/postgresql/data",
            "--env", f"POSTGRES_USER={db['user']}",
            "--env", f"POSTGRES_PASSWORD={db['password']}",
            "--env", f"POSTGRES_DB={DATABASE}",
            IMAGE,
        )  # fmt: skip
    elif state != "running":
        docker("start", CONTAINER)
    for _ in range(60):
        ready = subprocess.run(
            ["docker", "exec", CONTAINER, "pg_isready", "-U", db["user"], "-d", DATABASE],
            capture_output=True,
        )
        if ready.returncode == 0:
            return db
        time.sleep(1)
    raise RuntimeError("The local analysis database did not start within a minute")


SCHEMAS = ("ref.sql", "finance.sql", "census.sql")


def apply_schemas(conn):
    """Create the analysis schemas' missing tables (each file is safe to run again)."""
    folder = Path(__file__).with_name("sql")
    for name in SCHEMAS:
        conn.execute((folder / name).read_text("utf-8"))


def connect(db=None, **options):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(**(db or settings()), row_factory=dict_row, **options)
