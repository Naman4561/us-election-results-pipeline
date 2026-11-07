"""Oregon Secretary of State results: statewide and per-county CSV exports.

Oregon's "Statewide and County Exports" page (``export.html``) offers, in CSV format,
a Statewide Export (U.S. Senate and House), a Governor Export, and one export per county
with every contest on that county's ballot. The collector selects the page's own "CSV"
option and clicks each of those links, as a person would.

Checks: the page belongs to this election; statewide rows are single-seat contests with
one ID each; every county file lists only candidates the statewide file lists, under the
same contest ID; and county counts add up exactly to the statewide counts.

Oregon votes by mail and reports precincts as "0 of N" while ballots are counted, so
precinct counts are not stored. Candidates may be nominated by several parties
("Democrat, Working Families"); the first listed party is stored for map colors, as
DEM/REP/LIB/GRN where it is one of those. "Write-in" is published as one line per
contest and kept as a candidate.
"""

import csv
import io
import re
import zipfile

from election_pipeline.parsers.enr_platform import export_target, page_text
from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

HEADER = (
    "ContestID,ContestName,NominatingParty,PartyCode,AreaType,AreaNum,OfficeSeqNo,BallotOrder,"
    "CandidateID,CandidateName,CurrentDateTime,VoteFor,CandidateVotes,CandidatePercentage,"
    "PrecinctsReporting"
).split(",")
CHANGE_MEMBERS = ("export.html",)
CSV_OPTION = {"ctl00$MainContent$rblTypes": "1"}
STATEWIDE = {"statewide.csv": "Statewide Export", "governor.csv": "Governor Export"}
COUNTIES = (
    *"Baker Benton Clackamas Clatsop Columbia Coos Crook Curry Deschutes Douglas".split(),
    *"Gilliam Grant Harney".split(),
    "Hood River",
    *"Jackson Jefferson Josephine Klamath Lake Lane Lincoln Linn Malheur Marion".split(),
    *"Morrow Multnomah Polk Sherman Tillamook Umatilla Union Wallowa Wasco".split(),
    *"Washington Wheeler Yamhill".split(),
)
PARTIES = {"Democrat": "DEM", "Republican": "REP", "Libertarian": "LIB", "Pacific Green": "GRN"}
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 6: "6th"}


def member(county):
    return "county-" + county.lower().replace(" ", "-") + ".csv"


def contest(name):
    if name == "US Senator":
        return "OR-SEN"
    if name == "Governor":
        return "OR-GOV"
    match = re.fullmatch(r"US Representative, ([1-6])(st|nd|rd|th) District", name)
    if match and ORDINAL[int(match[1])] == match[1] + match[2]:
        return f"OR-0{match[1]}"
    return None


def exports(paths, source):
    page = paths["export.html"].read_text(encoding="utf-8", errors="replace")
    plan = [
        (name, "export.html", export_target(page, link_text=label), CSV_OPTION)
        for name, label in STATEWIDE.items()
    ]
    plan += [
        (member(county), "export.html", export_target(page, link_text=county), CSV_OPTION)
        for county in COUNTIES
    ]
    return plan


def records(raw, label):
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError(f"{label} contains NUL bytes")
    reader = csv.reader(io.StringIO(text), strict=True)
    if next(reader, []) != HEADER:
        raise ValueError(f"Unexpected Oregon CSV header in {label}")
    for line, values in enumerate(reader, 2):
        if len(values) == len(HEADER) + 1 and values[-1] == "":
            values.pop()
        if len(values) != len(HEADER) or line > 100_000:
            raise ValueError(f"Unexpected Oregon field count in {label}")
        values = dict(zip(HEADER, (" ".join(v.split()) for v in values)))
        for field in ("ContestID", "CandidateID", "CandidateVotes"):
            if not re.fullmatch(r"0|[1-9][0-9]{0,11}", values[field]):
                raise ValueError(f"{label}: {field} must be a nonnegative integer")
        yield values


def party(nominating):
    first = nominating.split(",")[0].strip()
    return PARTIES.get(first, first.upper())


def selected(raw, label, wanted):
    """{race: (contest ID, {candidate ID: (name, nominating parties, votes)})}."""
    found = {}
    for values in records(raw, label):
        race = contest(values["ContestName"])
        if race not in wanted:
            continue
        if values["VoteFor"] != "1":
            raise ValueError(f"{race}: not a single-seat Oregon contest")
        identity, candidates = found.setdefault(race, (values["ContestID"], {}))
        if identity != values["ContestID"]:
            raise ValueError(f"{race}: multiple Oregon contest IDs in {label}")
        entry = (values["CandidateName"], values["NominatingParty"], int(values["CandidateVotes"]))
        if not entry[0] or candidates.get(values["CandidateID"], entry) != entry:
            raise ValueError(f"{race}: conflicting Oregon candidate rows in {label}")
        candidates[values["CandidateID"]] = entry
    return found


def update_text(page):
    body = page_text(page)
    if (
        "Secretary of State, Oregon" not in body
        or "General Election November 3, 2026" not in body
        or "Statewide and County Exports" not in body
    ):
        raise ValueError("Oregon export page belongs to another election")
    found = set(
        re.findall(
            r"Results last updated: ([0-9]{1,2}/[0-9]{1,2}/2026 \| [0-9]{1,2}:[0-9]{2} [AP]M PT)",
            body,
        )
    )
    if len(found) != 1:
        raise ValueError("Oregon export page lacks one update time")
    links = {" ".join(a.text.split()) for a in Tree(page).root.find("a")}
    if not set(COUNTIES) <= links:
        raise ValueError("Oregon export page lacks a county export")
    return found.pop()


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "OR":
        raise ValueError("Unsupported Oregon source/election")
    wanted = state_adapter("OR").selected_races(source)
    with zipfile.ZipFile(archive_path) as archive:
        updated = update_text(archive.read("export.html").decode("utf-8", errors="replace"))
        official = {}
        for name in STATEWIDE:
            for race, value in selected(archive.read(name), name, wanted).items():
                if race in official:
                    raise ValueError(f"{race}: appears in more than one Oregon statewide file")
                official[race] = value
        counties = {c: selected(archive.read(member(c)), member(c), wanted) for c in COUNTIES}
    if set(official) != set(wanted):
        raise ValueError("Oregon statewide exports lack a selected contest")
    rows = []
    for race, (identity, candidates) in official.items():
        for candidate, (name, nominating, votes) in candidates.items():
            rows.append(row(race, identity, candidate, name, party(nominating), votes))
        places = []
        for county, found in counties.items():
            if race not in found:
                continue
            local_identity, local = found[race]
            if local_identity != identity:
                raise ValueError(f"{race} {county}: county file uses another contest ID")
            places.append(county)
            for candidate, (name, nominating, votes) in local.items():
                if candidates.get(candidate, (None, None))[:2] != (name, nominating):
                    raise ValueError(f"{race} {county}: candidate differs from statewide file")
                rows.append(
                    row(race, identity, candidate, name, party(nominating), votes, county=county)
                )
        verify_scope("OR", race, list(candidates), places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        contest_total_reconciliation="passed",
        vote_methods="not_published",
    )
