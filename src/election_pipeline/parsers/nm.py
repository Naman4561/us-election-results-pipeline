"""New Mexico Secretary of State results: statewide CSVs plus each race's County Level export.

``fed.csv`` and ``sw.csv`` (resultsCSV.aspx, type=FED and type=SW) give statewide totals,
candidate IDs, parties and precincts for U.S. Senate, U.S. House and governor. For each
race, ``county-<contest>.xlsx`` is that race's official "County Level" export from
``fed.html`` or ``sw.html``. Its Total sheet's TOTALS row and county sum must equal the
CSV. The export also has Absentee, Early and ElectionDay sheets; while New Mexico prints
``*`` there, vote methods are recorded as not reported. If New Mexico fills them in, they
must reconcile with the totals, but this totals-only contract does not store them yet.
"""

import re
import zipfile

from election_pipeline.parsers.enr_platform import (
    county_sheet,
    export_target,
    find_sheet,
    name_key,
    page_text,
    read_csv,
    sheet_rows,
    statewide_update,
)
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.status import results_status
from election_pipeline.parsers.structured import row
from election_pipeline.parsers.workbook import Workbook

HEADER = (
    "RaceID,RaceName,PartyCode,AreaNum,CandidateID,CandidateName,VoteFor,CandidateVotes,"
    "CandidatePercentage,PrecinctsReporting,CandidateAbsenteeVotes,CandidateElectionDayVotes,"
    "CandidateEarlyVotes"
).split(",")
CHANGE_MEMBERS = ("fed.csv", "sw.csv")
TITLE = "2026 {} General Election Results"
LABEL = "{} Results GENERAL 2026 November 3, 2026"
STATE_LINE = "State of New Mexico"
METHODS = ("Absentee", "Early", "ElectionDay")


def contest(values):
    name, area = " ".join(values["RaceName"].split()), values["AreaNum"]
    if name == "United States Senator" and not area:
        return "NM-SEN"
    if name == "Governor and Lieutenant Governor" and not area:
        return "NM-GOV"
    match = re.fullmatch(r"DISTRICT ([1-3])", area)
    if name == "United States Representative" and match:
        return f"NM-0{match[1]}"
    return None


def races(members, wanted):
    """Statewide CSV rows for the selected races, keyed by contest."""
    found = {}
    for member in CHANGE_MEMBERS:
        for line, values in read_csv(members[member], HEADER, "New Mexico"):
            race = contest(values)
            if race not in wanted:
                continue
            if values["VoteFor"] != "1":
                raise ValueError("New Mexico result is not a single-choice race")
            if found.get(race) and found[race][0]["_member"] != member:
                raise ValueError(f"{race}: appears in more than one New Mexico CSV")
            found.setdefault(race, []).append({**values, "_member": member})
    for race, rows in found.items():
        if len({r["RaceID"] for r in rows}) != 1:
            raise ValueError(f"{race}: multiple New Mexico race IDs")
    if set(found) != set(wanted):
        raise ValueError("New Mexico CSVs lack a selected race")
    return found


def member(race):
    return f"county-{race.lower()}.xlsx"


def page_for(race):
    return "sw.html" if race == "NM-GOV" else "fed.html"


def exports(paths, source):
    from election_pipeline.states import state_adapter

    wanted = state_adapter("NM").races
    found = races({m: paths[m].read_bytes() for m in CHANGE_MEMBERS}, wanted)
    plan = []
    for race in sorted(found):
        page = paths[page_for(race)].read_text(encoding="utf-8", errors="replace")
        target = export_target(page, button_value="County Level", race_id=found[race][0]["RaceID"])
        plan.append((member(race), page_for(race), target))
    return plan


def sheet_name(race, csv_name, area):
    """Workbook race label: House sheets name the district first, headers last."""
    if race.startswith("NM-0"):
        return f"{area} {csv_name}", f"{csv_name} {area}"
    return csv_name, csv_name


def methods_state(book, prefix_name, header_name, totals_by_county, title):
    """'not_reported' while every method cell is ``*``; otherwise they must reconcile."""
    reported = False
    method_cells = []
    for method in METHODS:
        sheet = find_sheet(book, method, prefix_name)
        names, counties, totals = sheet_rows(
            book, sheet, header_name, title=title, state_line=STATE_LINE, unreported=True
        )
        if set(counties) != set(totals_by_county):
            raise ValueError(f"{header_name}: {method} sheet covers different counties")
        method_cells.append((names, counties))
        reported |= any(v is not None for cells in counties.values() for v in cells)
        reported |= any(totals)
    if not reported:
        return "not_reported"
    for county, total in totals_by_county.items():
        for index, name in enumerate(method_cells[0][0]):
            parts = [counties[county][index] for _, counties in method_cells]
            if None not in parts and total.get(name) is not None:
                if sum(parts) != total[name]:
                    raise ValueError(f"{header_name}: {county} vote methods do not reconcile")
    return "reported_not_stored"


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "NM":
        raise ValueError("Unsupported New Mexico source/election")
    wanted = state_adapter("NM").races
    with zipfile.ZipFile(archive_path) as archive:
        pages = [archive.read(p).decode("utf-8", errors="replace") for p in ("fed.html", "sw.html")]
        updates = {
            statewide_update(page, "New Mexico", "GENERAL 2026 November 3, 2026") for page in pages
        }
        statuses = {results_status(page_text(page), LABEL, "New Mexico") for page in pages}
        found = races({m: archive.read(m) for m in CHANGE_MEMBERS}, wanted)
        books = {race: Workbook(archive.read(member(race)), settings) for race in found}
    if len(statuses) != 1:
        raise ValueError("New Mexico results pages disagree on whether results are official")
    official = statuses.pop()
    title = (TITLE.format("Official" if official else "Unofficial"),)
    output_rows, hidden_counties, method_status = [], {}, {}
    for race, csv_rows in sorted(found.items()):
        race_id = csv_rows[0]["RaceID"]
        csv_name = " ".join(csv_rows[0]["RaceName"].split())
        candidates = {}
        for values in csv_rows:
            match = re.fullmatch(r"([0-9]+)/([0-9]+)", values["PrecinctsReporting"])
            if not match or int(match[1]) > int(match[2]):
                raise ValueError("Unexpected New Mexico precinct counts")
            for field in (
                "CandidateAbsenteeVotes",
                "CandidateElectionDayVotes",
                "CandidateEarlyVotes",
            ):
                if values[field] and not values[field].isdigit():
                    raise ValueError("Unexpected New Mexico vote-method cell")
            name = " ".join(values["CandidateName"].split())
            if name in candidates:
                raise ValueError(f"{race}: duplicate New Mexico candidate")
            candidates[name] = (
                values["CandidateID"],
                values["PartyCode"],
                int(values["CandidateVotes"]),
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
        prefix_name, header_name = sheet_name(race, csv_name, csv_rows[0]["AreaNum"])
        book = books[race]
        counties, hidden = county_sheet(
            book,
            find_sheet(book, "Total", prefix_name),
            header_name,
            {name: votes for name, (_, _, votes) in candidates.items()},
            title=title,
            state_line=STATE_LINE,
            masked=True,
        )
        totals_by_county = {
            **{c: {name_key(n): v for n, v in votes.items()} for c, votes in counties.items()},
            **{county: {} for county in hidden},
        }
        method_status[race] = methods_state(book, prefix_name, header_name, totals_by_county, title)
        verify_scope("NM", race, [c[0] for c in candidates.values()], [*counties, *hidden])
        if hidden:
            hidden_counties[race] = hidden
        for county, votes in counties.items():
            for name, count_ in votes.items():
                identity, party, _ = candidates[name]
                output_rows.append(row(race, race_id, identity, name, party, count_, county=county))
    if len(updates) != 1:
        raise ValueError("New Mexico results pages show different update times")
    finish(
        output_rows,
        source,
        settings,
        output,
        stats,
        reference=reference,
        reconcile=not hidden_counties,
    )
    stats.update(
        source_update_text=updates.pop(),
        official_results=official,
        contest_total_reconciliation="passed",
        county_reconciliation=(
            "passed_except_privacy_masked_counties" if hidden_counties else "passed"
        ),
        privacy_masked_counties=hidden_counties,
        vote_method_status=method_status,
    )
