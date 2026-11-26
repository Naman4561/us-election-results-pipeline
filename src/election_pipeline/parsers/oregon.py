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

from election_pipeline import parse_report
from election_pipeline.parsers.enr_platform import export_target, page_text
from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.status import results_status
from election_pipeline.parsers.structured import row

HEADER = (
    "ContestID,ContestName,NominatingParty,PartyCode,AreaType,AreaNum,OfficeSeqNo,BallotOrder,"
    "CandidateID,CandidateName,CurrentDateTime,VoteFor,CandidateVotes,CandidatePercentage,"
    "PrecinctsReporting"
).split(",")
CHANGE_MEMBERS = ("export.html",)
CSV_OPTION = {"ctl00$MainContent$rblTypes": "1"}
STATEWIDE = {"statewide.csv": "Statewide Export", "governor.csv": "Governor Export"}


def member(county):
    return "county-" + county.lower().replace(" ", "-") + ".csv"


def exports(paths, source):
    from election_pipeline.platforms import enr

    counties = enr(source).counties
    page = paths["export.html"].read_text(encoding="utf-8", errors="replace")
    plan = [
        (name, "export.html", export_target(page, link_text=label), CSV_OPTION)
        for name, label in STATEWIDE.items()
    ]
    plan += [
        (member(county), "export.html", export_target(page, link_text=county), CSV_OPTION)
        for county in counties
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


def party_of(nominating):
    """The first party a candidate is nominated by."""
    return nominating.split(",")[0].strip()


def selected(raw, label, site, wanted):
    """{race: (contest ID, {candidate ID: (name, nominating parties, votes)})}."""
    found = {}
    for values in records(raw, label):
        race = site.race(values["ContestName"])
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


def update_text(page, site, source):
    """The page's update time, and whether it labels the results official."""
    body = page_text(page)
    if (
        site.page_identity not in body
        or site.page_election not in body
        or "Statewide and County Exports" not in body
    ):
        raise ValueError("Oregon export page belongs to another election")
    year = source.election_date[:4]
    found = set(
        re.findall(
            rf"Results last updated: ([0-9]{{1,2}}/[0-9]{{1,2}}/{year} \| "
            r"[0-9]{1,2}:[0-9]{2} [AP]M PT)",
            body,
        )
    )
    if len(found) != 1:
        raise ValueError("Oregon export page lacks one update time")
    links = {" ".join(a.text.split()) for a in Tree(page).root.find("a")}
    if not set(site.counties) <= links:
        raise ValueError("Oregon export page lacks a county export")
    return found.pop(), results_status(body, site.status_label, site.name)


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.platforms import enr
    from election_pipeline.states import state_adapter

    site = enr(source)
    if source.format != "bundle":
        raise ValueError("Unsupported Oregon source/election")
    wanted = state_adapter(source.state).races
    with zipfile.ZipFile(archive_path) as archive:
        updated, final = update_text(
            archive.read("export.html").decode("utf-8", errors="replace"), site, source
        )
        official = {}
        for name in STATEWIDE:
            for race, value in selected(archive.read(name), name, site, wanted).items():
                if race in official:
                    raise ValueError(f"{race}: appears in more than one Oregon statewide file")
                official[race] = value
        counties = {
            c: selected(archive.read(member(c)), member(c), site, wanted) for c in site.counties
        }
    if set(official) != set(wanted):
        raise ValueError("Oregon statewide exports lack a selected contest")
    rows = []
    for race, (identity, candidates) in official.items():
        for candidate, (name, nominating, votes) in candidates.items():
            rows.append(
                row(race, identity, candidate, name, site.party(party_of(nominating)), votes)
            )
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
                    row(
                        race,
                        identity,
                        candidate,
                        name,
                        site.party(party_of(nominating)),
                        votes,
                        county=county,
                    )
                )
        verify_scope(source.state, race, list(candidates), places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        **parse_report.fields(final),
        contest_total_reconciliation="passed",
    )
