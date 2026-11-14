"""Download outside data once, keep the original bytes, and record where they came from.

Each file is stored under ``ANALYSIS_HOME/raw/<name>/<sha256><suffix>`` exactly as
received and registered in ``ref.datasets`` (URL, download time, size, hash, license,
vintage) before any table is loaded from it. A loader reads only that stored copy, so a
published number can always be traced to the bytes it came from.
"""

import csv
import hashlib
import io
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from analysis import local

AGENT = "election-analysis/0.1 (research; contact via repository)"


def download(conn, name, url, *, license, vintage=None, suffix=""):
    """(dataset_id, path) of ``url``'s bytes, registered in ref.datasets."""
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if not data:
        raise ValueError(f"{url} returned no data")
    digest = hashlib.sha256(data).hexdigest()
    path = local.HOME / "raw" / name / (digest + suffix)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        partial.write_bytes(data)
        partial.replace(path)
    dataset = f"{name}:{digest[:12]}"
    conn.execute(
        "INSERT INTO ref.datasets"
        " (dataset_id, name, source_url, downloaded_at, sha256, bytes, license, vintage)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dataset_id) DO NOTHING",
        (dataset, name, url, datetime.now(timezone.utc), digest, len(data), license, vintage),
    )
    return dataset, path


COUNTIES = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"


def counties(conn):
    """The Census Bureau's 2020 county list: every county and equivalent, by FIPS."""
    dataset, path = download(
        conn, "census_counties", COUNTIES, license="Public domain (U.S. Census Bureau)",
        vintage="2020", suffix=".txt",
    )  # fmt: skip
    rows = list(csv.DictReader(io.StringIO(Path(path).read_text("utf-8")), delimiter="|"))
    if not rows or set(rows[0]) != {
        "STATE", "STATEFP", "COUNTYFP", "COUNTYNS", "COUNTYNAME", "CLASSFP", "FUNCSTAT",
    }:  # fmt: skip
        raise ValueError("Unexpected Census county list format")
    with conn.transaction():
        conn.execute("DELETE FROM ref.county_aliases")
        conn.execute("DELETE FROM ref.counties")
        with conn.cursor().copy(
            "COPY ref.counties (fips, state, name, dataset_id) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(
                    (row["STATEFP"] + row["COUNTYFP"], row["STATE"], row["COUNTYNAME"], dataset)
                )
    return {"dataset_id": dataset, "counties": len(rows)}
