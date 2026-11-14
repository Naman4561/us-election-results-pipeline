"""Build and test the analytics layer: python -m analysis build

Runs ``dbt build`` (seeds, models and their tests) on analysis/dbt against the local
analysis database. Any failing test fails the build; the tables it built are left for
inspection, but findings should come only from a passing build.
"""

import os
import subprocess
import sys
from pathlib import Path

from analysis import local

PROJECT = Path(__file__).with_name("dbt")


def build(*arguments):
    db = local.start()
    env = {
        **os.environ,
        "ANALYSIS_HOST": db["host"],
        "ANALYSIS_PORT": str(db["port"]),
        "ANALYSIS_USER": db["user"],
        "ANALYSIS_PASSWORD": db["password"],
        "ANALYSIS_DBNAME": db["dbname"],
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    dbt = Path(sys.executable).with_name("dbt.exe" if os.name == "nt" else "dbt")
    command = [str(dbt), "build", "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
    return subprocess.call([*command, *arguments], env=env, cwd=PROJECT)
