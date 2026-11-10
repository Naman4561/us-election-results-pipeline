"""South Dakota Secretary of State results: statewide CSV plus the official statewide export.

``results.csv`` (resultsCSV.aspx, type=SWR) gives each race's statewide totals, candidate
IDs, parties and precincts. ``statewide.xlsx`` is the page's "Statewide Export" workbook:
one sheet per race with a row per county and a TOTALS row. Every county row is kept; the
TOTALS row and the county sum must both equal the CSV, or the snapshot is rejected.
South Dakota does not publish vote-method (ballot type) detail on this site.
"""

import re
import zipfile
from datetime import datetime

from election_pipeline.parsers.enr_platform import (
    county_sheet,
    export_target,
    find_sheet,
    page_text,
    read_csv,
    statewide_update,
)
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.status import results_status
from election_pipeline.parsers.structured import row
from election_pipeline.parsers.workbook import Workbook

HEADER = (
    "RaceID,RaceName,PartyCode,AreaType,AreaNum,OfficeSeqNo,BallotOrder,CandidateID,"
    "CandidateName,CurrentDateTime,VoteFor,CandidateVotes,CandidatePercentage,PrecinctsReporting"
).split(",")
RACES = {
    "United States Senator": "SD-SEN",
    "United States Representative": "SD-AL",
    "Governor and Lieutenant Governor": "SD-GOV",
}
CHANGE_MEMBERS = ("results.csv",)
EXPORT = "statewide.xlsx"
TITLE = "2026 {} General Election Results"
LABEL = "{} Results General Election November 3, 2026"


def races(raw, wanted):
    """Statewide CSV rows for the selected races, keyed by contest."""
    found = {}
    for line, values in read_csv(raw, HEADER, "South Dakota"):
        race = RACES.get(" ".join(values["RaceName"].split()))
        if race not in wanted:
            continue
        if values["AreaType"] or values["AreaNum"] or values["VoteFor"] != "1":
            raise ValueError("South Dakota result is not a single-choice statewide total")
        found.setdefault(race, []).append(values)
    for race, rows in found.items():
        if len({r["RaceID"] for r in rows}) != 1:
            raise ValueError(f"{race}: multiple South Dakota race IDs")
    if set(found) != set(wanted):
        raise ValueError("South Dakota CSV lacks a selected race")
    return found


def exports(paths, source):
    page = paths["export.html"].read_text(encoding="utf-8", errors="replace")
    return [(EXPORT, "export.html", export_target(page, link_text="Statewide Export"))]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "SD":
        raise ValueError("Unsupported South Dakota source/election")
    wanted = state_adapter("SD").races
    with zipfile.ZipFile(archive_path) as archive:
        page = archive.read("export.html").decode("utf-8", errors="replace")
        updated = statewide_update(page, "South Dakota", "General Election November 3, 2026")
        official = results_status(page_text(page), LABEL, "South Dakota")
        found = races(archive.read(source.primary_filename), wanted)
        book = Workbook(archive.read(EXPORT), settings)
    stamps = {r["CurrentDateTime"] for rows in found.values() for r in rows}
    if len(stamps) != 1:
        raise ValueError("South Dakota races carry different update times")
    if not (
        datetime(2026, 11, 3)
        <= datetime.strptime(stamps.pop(), "%m/%d/%Y %I:%M:%S %p")
        <= datetime(2026, 12, 31)
    ):
        raise ValueError("South Dakota update time is outside this election's window")
    output_rows = []
    for race, csv_rows in found.items():
        race_id = csv_rows[0]["RaceID"]
        candidates = {}
        for values in csv_rows:
            match = re.fullmatch(r'="?([0-9]+)/([0-9]+)"?', values["PrecinctsReporting"])
            if not match or int(match[1]) > int(match[2]):
                raise ValueError("Unexpected South Dakota precinct counts")
            name = " ".join(values["CandidateName"].split())
            candidates[name] = (
                values["CandidateID"],
                values["PartyCode"],
                values["CandidateVotes"],
            )
            output_rows.append(
                row(
                    race,
                    race_id,
                    values["CandidateID"],
                    name,
                    values["PartyCode"],
                    int(values["CandidateVotes"]),
                    reporting=int(match[1]),
                    total=int(match[2]),
                )
            )
        race_name = " ".join(csv_rows[0]["RaceName"].split())
        counties, _ = county_sheet(
            book,
            find_sheet(book, race_id, race_name),
            race_name,
            {name: int(votes) for name, (_, _, votes) in candidates.items()},
            title=(TITLE.format("Official" if official else "Unofficial"),),
            state_line="State of South Dakota",
        )
        verify_scope("SD", race, [c[0] for c in candidates.values()], counties)
        for county, votes in counties.items():
            for name, count in votes.items():
                identity, party, _ = candidates[name]
                output_rows.append(row(race, race_id, identity, name, party, count, county=county))
    finish(output_rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        official_results=official,
        contest_total_reconciliation="passed",
    )
