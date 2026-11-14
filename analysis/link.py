"""Match each state's county spellings in the results to Census FIPS codes.

A spelling matches exactly (ignoring case and a trailing "County"), or after
normalization (punctuation and spaces removed, "Saint" as "St", the county-type word
dropped), or through a reviewed row of ``analysis/ref/county_aliases.csv``. Independent
cities keep their "city" ("Richmond city" is not Richmond County). Anything else is
reported, never guessed.
"""

import csv
import re
import unicodedata
from pathlib import Path

from analysis import registry

REVIEWED = Path(__file__).with_name("ref") / "county_aliases.csv"
KINDS = re.compile(r"\s+(county|parish|borough|census area|city and borough|municipality)$", re.I)


def plain(name):
    return KINDS.sub("", name.strip()).casefold()


def normalized(name):
    # "Doña Ana" and "DONA ANA" are one county.
    text = unicodedata.normalize("NFKD", plain(name)).encode("ascii", "ignore").decode()
    text = text.replace("&", "and")
    text = re.sub(r"\bsaint\b", "st", text)
    return re.sub(r"[^a-z0-9]", "", text)


def reviewed():
    if not REVIEWED.exists():
        return {}
    with REVIEWED.open(encoding="utf-8", newline="") as handle:
        return {(row["state"], row["county"]): row["fips"] for row in csv.DictReader(handle)}


def matches(spellings, counties, manual=None):
    """({(state, county): (fips, method)}, [(state, county) unmatched])."""
    manual = manual or {}
    exact, loose = {}, {}
    for fips, state, name in counties:
        exact.setdefault((state, plain(name)), []).append(fips)
        loose.setdefault((state, normalized(name)), []).append(fips)
    found, missing = {}, []
    for state, county in spellings:
        if (state, county) in manual:
            found[state, county] = (manual[state, county], "reviewed")
            continue
        for index, method, key in ((exact, "exact", plain), (loose, "normalized", normalized)):
            candidates = index.get((state, key(county)), [])
            if len(candidates) == 1:
                found[state, county] = (candidates[0], method)
                break
        else:
            missing.append((state, county))
    return found, missing


def by_county():
    """The states whose local results are counties, from the pipeline's own registry
    (Vermont's are towns: a town named like its county is not that county)."""
    return {code for code, state in registry.states().items() if state.local_unit == "county"}


def link(conn):
    states = by_county()
    spellings = [
        (row["state"], row["county"])
        for row in conn.execute(
            # Every publication, of every source, not only the current ones: history and
            # older sources join on FIPS too.
            "SELECT DISTINCT p.state, r.county FROM serving.publications p"
            " JOIN serving.county_results r USING (publication_id) ORDER BY 1, 2"
        ).fetchall()
        if row["state"] in states
    ]
    counties = [
        (row["fips"], row["state"], row["name"])
        for row in conn.execute("SELECT fips, state, name FROM ref.counties").fetchall()
    ]
    if not counties:
        raise ValueError("Load the county list first: python -m analysis fetch counties")
    found, missing = matches(spellings, counties, reviewed())
    with conn.transaction():
        conn.execute("DELETE FROM ref.county_aliases")
        with conn.cursor().copy(
            "COPY ref.county_aliases (state, county, fips, method) FROM STDIN"
        ) as copy:
            for (state, county), (fips, method) in sorted(found.items()):
                copy.write_row((state, county, fips, method))
    return found, missing
