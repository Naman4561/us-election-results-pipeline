"""New Hampshire Secretary of State: the general election's results spreadsheets, by hand.

The results site (sos.nh.gov) refuses automated clients, so the operator downloads its
four spreadsheets and imports them, unchanged, as one zip (``results.zip``) of:

- ``2026-ge-us-senator-summary.xlsx`` and ``2026-ge-governor-summary.xlsx``: a row per
  county ("Summary By Counties") and a TOTALS row;
- ``2026-ge-congressional-district-1.xlsx`` and ``-2.xlsx``: a row per town, city ward
  or unincorporated place, and a TOTALS row.

A candidate's heading is its name and party letter ("Kelly Ayotte, r"); write-ins are
one "Write-in" line. Every sheet's rows must add up to its TOTALS, column by column, and
a sheet's blank columns must hold no votes. House towns are added up into their counties
(``resources/nh_2026_towns.json``; a ward counts for its city), so every race is shown
by county. The spreadsheets print no report time: the operator declares it at import.
"""

import io
import json
import re
import zipfile

from election_pipeline import elections
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row
from election_pipeline.parsers.workbook import Workbook

MEMBERS = {
    "2026-ge-us-senator-summary.xlsx": ("NH-SEN", "United States Senator"),
    "2026-ge-governor-summary.xlsx": ("NH-GOV", "Governor"),
    "2026-ge-congressional-district-1.xlsx": ("NH-01", "Congressional District 1"),
    "2026-ge-congressional-district-2.xlsx": ("NH-02", "Congressional District 2"),
}
TITLE = "State of New Hampshire - General Election"
# The import zip's layout, for scripts/pack_import.py (election_pipeline.packing).
PACK = {"members": tuple(MEMBERS)}
DATE = "November 03, 2026"
PARTIES = {"d": "DEM", "r": "REP", "l": "LIB", "c": "CON", "i": "IND", "g": "GRN"}
COUNTIES = (
    "Belknap", "Carroll", "Cheshire", "Coos", "Grafton", "Hillsborough", "Merrimack",
    "Rockingham", "Strafford", "Sullivan",
)  # fmt: skip
WRITE_IN = ("write-in", "Write-in")
MAX_BYTES = 5_000_000


def towns():
    raw = elections.table("nh-towns.json").decode("utf-8")
    table = json.loads(raw)["towns"]
    if set(table.values()) != set(COUNTIES):
        raise ValueError("New Hampshire town table names an unknown county")
    return table


def number(value, label):
    text = "0" if value in (None, "") else str(value)
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise ValueError(f"New Hampshire results: invalid {label} {value!r}")
    return count(int(text))


def heading(cell, race):
    """(name, party) from a candidate heading; None for a blank column."""
    if cell in (None, ""):
        return None
    if " ".join(str(cell).split()) == "Write-Ins":
        return WRITE_IN[1], ""
    match = re.fullmatch(r"(.+), ([a-z])", " ".join(str(cell).split()))
    if not match or match[2] not in PARTIES:
        raise ValueError(f"{race}: unexpected New Hampshire candidate heading {cell!r}")
    return match[1], PARTIES[match[2]]


def sheet(book, race, office):
    """([(name, party) or None per column], {place: [votes]}, totals) for one sheet."""
    (name,) = book.sheets
    rows = [r for r in book.rows(name, sums=True) if any(v not in (None, "") for v in r)]
    if len(rows) < 5:
        raise ValueError(f"{race}: New Hampshire sheet is too short")
    county_summary = race in ("NH-SEN", "NH-GOV")
    title, label = (rows[0][1], rows[1][1]) if county_summary else (rows[0][0], rows[1][0])
    date = rows[1][0] if county_summary else rows[2][0]
    if (
        title != TITLE
        or label != office
        or str(date).upper() != DATE.upper()
        or (county_summary and rows[2][0] != "Summary By Counties")
    ):
        raise ValueError(f"{race}: New Hampshire sheet belongs to another office or election")
    columns = [heading(cell, race) for cell in rows[2][1:]]
    width = len(rows[2])
    places, totals = {}, None
    for values in rows[3:]:
        values = values + [None] * (width - len(values))
        if len(values) != width or totals is not None:
            raise ValueError(f"{race}: unexpected New Hampshire row after TOTALS or width")
        place = " ".join(str(values[0] or "").split())
        votes = [number(v, "votes") for v in values[1:]]
        if any(v for v, c in zip(votes, columns) if c is None):
            raise ValueError(f"{race}: New Hampshire votes in a blank column")
        if place == "TOTALS":
            totals = votes
        elif not place or place in places:
            raise ValueError(f"{race}: missing or repeated New Hampshire place {place!r}")
        else:
            places[place] = votes
    if totals is None:
        raise ValueError(f"{race}: New Hampshire sheet lacks its TOTALS row")
    if [sum(v[i] for v in places.values()) for i in range(len(columns))] != totals:
        raise ValueError(f"{race}: New Hampshire rows do not add up to the TOTALS")
    return columns, places, totals


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "NH":
        raise ValueError("Unsupported New Hampshire source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if {e.filename for e in entries} != set(MEMBERS) or len(entries) != len(MEMBERS):
            raise ValueError("New Hampshire import is not the four results spreadsheets")
        if any(e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries):
            raise ValueError("New Hampshire spreadsheet is too large or encrypted")
        books = {m: Workbook(bundle.read(m), settings) for m in MEMBERS}
    table = towns()
    wanted = state_adapter("NH").races
    if set(race for race, _ in MEMBERS.values()) != set(wanted):
        raise ValueError("New Hampshire spreadsheets do not cover its races")
    rows = []
    for member, (race, office) in MEMBERS.items():
        columns, places, totals = sheet(books[member], race, office)
        if race in ("NH-SEN", "NH-GOV"):
            if set(places) != set(COUNTIES):
                raise ValueError(f"{race}: New Hampshire summary lacks a county")
            counties = places
        else:
            counties = {}
            for place, votes in places.items():
                town = re.sub(r" Ward [0-9]+$", "", place)
                if town not in table:
                    raise ValueError(f"{race}: unknown New Hampshire place {place!r}")
                summed = counties.setdefault(table[town], [0] * len(votes))
                counties[table[town]] = [a + b for a, b in zip(summed, votes)]
        listed = [(i, c) for i, c in enumerate(columns) if c is not None]
        for i, (name, party) in listed:
            key = WRITE_IN[0] if not party else name
            rows.append(row(race, race, key, name, party, totals[i]))
            for county, votes in sorted(counties.items()):
                rows.append(row(race, race, key, name, party, votes[i], county))
        names = [name for _, (name, party) in listed if party]
        verify_scope("NH", race, names, sorted(counties))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(contest_total_reconciliation="passed")
