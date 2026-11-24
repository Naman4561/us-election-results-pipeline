"""Montana Secretary of State results: the results site's Statewide Export workbook.

``export.html`` is the site's export page (its "Results last updated" time and the
election's name); ``statewide.xlsx`` is its "Statewide Export": one sheet per contest,
with a row per county and a TOTALS row, under the election's title ("2026 Unofficial
General Election Results"), the state and a precincts-reporting line. Montana has no
statewide CSV, so the TOTALS row is the statewide count: the county rows must add up to
it, candidate by candidate. A candidate's heading is its name and, on its own line, its
party. The site reports no write-ins and no vote methods. (Collected by
``collectors.montana``.)
"""

import re
import zipfile

from election_pipeline.parsers.enr_platform import (
    export_target as platform_target,
)
from election_pipeline.parsers.enr_platform import name_key, page_text, sheet_rows
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.parsers.workbook import Workbook

EXPORT = "statewide.xlsx"
ELECTION = "2026 General Election - November 3, 2026"
UPDATED = re.compile(
    r"Results last updated: ([0-9]{1,2}/[0-9]{1,2}/2026 [0-9]{1,2}:[0-9]{2}:[0-9]{2} [AP]M MT)"
)
RACES = {
    "UNITED STATES SENATOR": "MT-SEN",
    "UNITED STATES REPRESENTATIVE 1ST CONGRESSIONAL": "MT-01",
    "UNITED STATES REPRESENTATIVE 2ND CONGRESSIONAL": "MT-02",
}
TITLE = "2026 {} General Election Results"
PARTIES = {
    "Republican": "REP",
    "Democrat": "DEM",
    "Libertarian": "LIB",
    "Green": "GRN",
    "Independent": "IND",
}
REPORTING = re.compile(r"Precincts ([0-9]+) of ([0-9]+) Reporting(?: \(.*\))?")
COUNTIES = (
    "Beaverhead", "Big Horn", "Blaine", "Broadwater", "Carbon", "Carter", "Cascade",
    "Chouteau", "Custer", "Daniels", "Dawson", "Deer Lodge", "Fallon", "Fergus", "Flathead",
    "Gallatin", "Garfield", "Glacier", "Golden Valley", "Granite", "Hill", "Jefferson",
    "Judith Basin", "Lake", "Lewis and Clark", "Liberty", "Lincoln", "Madison", "McCone",
    "Meagher", "Mineral", "Missoula", "Musselshell", "Park", "Petroleum", "Phillips",
    "Pondera", "Powder River", "Powell", "Prairie", "Ravalli", "Richland", "Roosevelt",
    "Rosebud", "Sanders", "Sheridan", "Silver Bow", "Stillwater", "Sweet Grass", "Teton",
    "Toole", "Treasure", "Valley", "Wheatland", "Wibaux", "Yellowstone",
)  # fmt: skip
# The export spells counties in capitals, Lewis and Clark as "LEWIS & CLARK".
NAMES = {name.upper().replace(" AND ", " & "): name for name in COUNTIES}


def export_target(page):
    return platform_target(page, link_text="Statewide Export")


def header(rows):
    """The one header row of a sheet: [race name, "County", candidate headings...], or
    None. A selected contest without one is missing, which the parse refuses."""
    found = [r for r in rows if len(r) > 2 and r[1] == "County"]
    return found[0] if len(found) == 1 else None


def candidates(cells):
    """[(name, party)] from candidate headings: a name, then its party on its own line."""
    found = []
    for cell in cells:
        lines = [line.strip() for line in str(cell or "").split("\n") if line.strip()]
        if len(lines) != 2 or lines[1] not in PARTIES:
            raise ValueError(f"Unexpected Montana candidate heading {cell!r}")
        found.append((name_key(lines[0]), PARTIES[lines[1]]))
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "MT":
        raise ValueError("Unsupported Montana source/election")
    wanted = state_adapter("MT").races
    with zipfile.ZipFile(archive_path) as archive:
        page = archive.read(source.primary_filename).decode("utf-8", errors="replace")
        book = Workbook(archive.read(EXPORT), settings)
    body = page_text(page)
    # Montana prints seconds, unlike the platform's other sites (statewide_update).
    times = set(UPDATED.findall(body))
    if "Montana Secretary of State" not in body or ELECTION not in body or len(times) != 1:
        raise ValueError("Montana export page lacks its election or one update time")
    if "Write-in results are not reported on this site" not in body:
        raise ValueError("Montana export page lacks its write-in notice")
    updated = times.pop()
    found, official = {}, set()
    for sheet in book.sheets:
        rows = book.rows(sheet)
        headings = header(rows)
        race_name = name_key(headings[0]) if headings else None
        race = RACES.get(race_name)
        if race is None:
            continue
        listed = candidates(headings[2:])
        if race in found:
            raise ValueError(f"{race}: Montana export repeats the contest")
        title = rows[0][0] if rows and rows[0] else None
        if title not in (TITLE.format("Unofficial"), TITLE.format("Official")):
            raise ValueError("Montana export belongs to another election")
        official.add(title == TITLE.format("Official"))
        reporting = REPORTING.fullmatch(str(rows[2][0] if len(rows) > 2 and rows[2] else ""))
        if not reporting or int(reporting[1]) > int(reporting[2]):
            raise ValueError(f"{race}: unexpected Montana precincts-reporting line")
        names, counties, totals = sheet_rows(
            book, sheet, race_name, title=(title,), state_line="State of Montana"
        )
        if names != [name_key(cell) for cell in headings[2:]]:
            raise ValueError(f"{race}: Montana candidate headings disagree")
        if [sum(cells[i] for cells in counties.values()) for i in range(len(names))] != totals:
            raise ValueError(f"{race}: Montana county rows do not add up to the TOTALS")
        unknown = set(counties) - set(NAMES)
        if unknown:
            raise ValueError(f"{race}: unknown Montana county {sorted(unknown)[0]!r}")
        found[race] = (listed, counties, totals, int(reporting[1]), int(reporting[2]))
    if set(found) != set(wanted):
        raise ValueError("Montana export lacks a selected contest")
    if len(official) != 1:
        raise ValueError("Montana export mixes unofficial and official sheets")
    if set(found["MT-SEN"][1]) != set(NAMES):
        raise ValueError("MT-SEN: Montana export lacks a county")
    rows = []
    for race, (listed, counties, totals, done, total) in found.items():
        for (name, party), votes in zip(listed, totals):
            rows.append(row(race, race, name, name, party, votes, reporting=done, total=total))
        for county, cells in counties.items():
            for (name, party), votes in zip(listed, cells):
                rows.append(row(race, race, name, name, party, votes, county=NAMES[county]))
        verify_scope("MT", race, [name for name, _ in listed], [NAMES[c] for c in counties])
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        official_results=official.pop(),
        contest_total_reconciliation="passed",
    )
