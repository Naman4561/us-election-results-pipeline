"""Load ACS 5-year estimates for counties and congressional districts.

The Census API now requires a key; the Bureau's table-based summary files do not. Each
table's file covers every geography down to block groups (tens to hundreds of MB); it is
kept as downloaded, and only its county (summary level 050) and congressional district
(500) rows are loaded. Public domain.
"""

import csv
import io
import re
from pathlib import Path

from analysis import fetch

RELEASE = "2024"
BASE = f"https://www2.census.gov/programs-surveys/acs/summary_file/{RELEASE}/table-based-SF/"
SHELLS = BASE + f"documentation/ACS{RELEASE}5YR_Table_Shells.txt"
LICENSE = "Public domain (U.S. Census Bureau)"
VINTAGE = "ACS 2020-2024 5-year"
# Total population, citizen voting-age population, median age, race and Hispanic origin,
# education (25 and over), median household income, poverty status.
TABLES = ("B01003", "B29001", "B01002", "B03002", "B15003", "B19013", "B17001")
# The 2020-2024 release reports congressional districts of the 119th Congress (the lines
# of the 2024 elections); states that redrew for 2026 need their new lines separately.
CONGRESS = 119
COUNTY = re.compile(r"0500000US(\d{5})")
# Summary level 500, the Congress's last two digits, component 00: "5001900US4001".
DISTRICT = re.compile(rf"500{CONGRESS % 100:02d}00US(\d{{4}})")
# Codes the Bureau puts in place of a number (see the summary file documentation).
NOT_PUBLISHED = {"-666666666", "-999999999", "-888888888", "-222222222", "-333333333"}
CONTROLLED = "-555555555"  # a margin of error for a total the Bureau controls: none


def estimate(value):
    if value in NOT_PUBLISHED or value == CONTROLLED or value in ("", "null"):
        return None
    return value


def margin(value):
    return "0" if value == CONTROLLED else estimate(value)


def shells(conn):
    dataset, path = fetch.download(
        conn, "acs5_2024_shells", SHELLS, license=LICENSE, vintage=VINTAGE, suffix=".txt"
    )
    present = conn.execute(
        "SELECT count(*) AS n FROM census.acs_variables WHERE dataset_id = %s", (dataset,)
    ).fetchone()["n"]
    if present:
        return present
    reader = csv.DictReader(io.StringIO(Path(path).read_bytes().decode("latin-1")), delimiter="|")
    if reader.fieldnames[:5] != ["Table ID", "Line", "Indent", "Unique ID", "Label"]:
        raise ValueError("Unexpected ACS table shells format")
    count = 0
    with conn.cursor().copy(
        "COPY census.acs_variables (dataset_id, variable, table_id, label) FROM STDIN"
    ) as copy:
        for row in reader:
            if row["Table ID"] in TABLES and row["Unique ID"]:
                copy.write_row((dataset, row["Unique ID"], row["Table ID"], row["Label"]))
                count += 1
    return count


def table(conn, table_id):
    """Download one table's file and load its county and district rows."""
    name = f"acs5_2024_{table_id.lower()}"
    url = BASE + f"data/5YRData/acsdt5y{RELEASE}-{table_id.lower()}.dat"
    dataset, path = fetch.download(conn, name, url, license=LICENSE, vintage=VINTAGE, suffix=".dat")
    present = conn.execute(
        "SELECT count(*) AS n FROM census.acs_estimates WHERE dataset_id = %s", (dataset,)
    ).fetchone()["n"]
    if present:
        return present
    with Path(path).open(encoding="latin-1", newline="") as handle:
        reader = csv.reader(handle, delimiter="|")
        header = next(reader)
        if header[0] != "GEO_ID" or not all(c.startswith(table_id + "_") for c in header[1:]):
            raise ValueError(f"Unexpected ACS {table_id} columns")
        # B03002_E001, B03002_M001, ...: an estimate and its margin per variable.
        variables = [(i, f"{table_id}_{header[i][-3:]}") for i in range(1, len(header), 2)]
        if any(header[i + 1] != f"{table_id}_M{header[i][-3:]}" for i, _ in variables):
            raise ValueError(f"ACS {table_id}: estimates and margins out of order")
        count = 0
        with conn.cursor().copy(
            "COPY census.acs_estimates (dataset_id, geo_type, geoid, congress, variable,"
            " estimate, moe) FROM STDIN"
        ) as copy:
            for row in reader:
                if county := COUNTY.fullmatch(row[0]):
                    kind, geoid, congress = "county", county[1], None
                elif district := DISTRICT.fullmatch(row[0]):
                    kind, geoid, congress = "congressional_district", district[1], CONGRESS
                else:
                    continue
                for i, variable in variables:
                    copy.write_row(
                        (
                            dataset,
                            kind,
                            geoid,
                            congress,
                            variable,
                            estimate(row[i]),
                            margin(row[i + 1]),
                        )
                    )
                    count += 1
    return count


def load(conn):
    loaded = {}
    with conn.transaction():
        loaded["shells"] = shells(conn)
    for table_id in TABLES:
        with conn.transaction():
            loaded[table_id] = table(conn, table_id)
    return loaded
