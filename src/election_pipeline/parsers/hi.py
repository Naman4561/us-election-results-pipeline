"""Hawaii Office of Elections results: statewide summary file with mail / in-person votes.

``summary.txt`` is the official tab-delimited "summary" results file. Each candidate row
gives Mail Votes, In-Person Votes and Total Votes; both methods are stored and must add up
to the total. Hawaii elects no U.S. senator in 2026.

County detail is not collected. Hawaii publishes it only per county as PDF reports, or
per precinct (``media.txt``) without naming each precinct's county; assigning precincts to
counties would be an inference this pipeline does not make.

Names are printed "(D) CASE, Ed"; the party letter becomes the party and the rest is kept
as published. Governor tickets are printed on four lines ("GREEN, Josh / For GOVERNOR /
KAWAKAMI, Derek S.K. / For LIEUTENANT GOVERNOR") and are stored as "GREEN, Josh /
KAWAKAMI, Derek S.K.".
"""

import csv
import io
import re
import zipfile

from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row, text
from election_pipeline.parsers.totals import finish, with_methods

HEADER = [
    "#Contest ID",
    "Contest Title",
    "Contest Seq Nbr",
    "Contest Type",
    "Contest Party",
    "Mail Blank Votes",
    "In-Person Blank Votes",
    "Mail Over Votes",
    "In-Person Over Votes",
    "Mail Invalid Votes",
    "In-Person Invalid Votes",
    "Registered Voters",
    "Total Precincts",
    "Counted Precincts",
    "Candidate ID",
    "Candidate Name",
    "Candidate Seq Nbr",
    "Candidate Party",
    "Mail Votes",
    "In-Person Votes",
    "Total Votes",
]
TITLES = {
    "Governor and Lieutenant Governor": "HI-GOV",
    "U.S. Representative, Dist I": "HI-01",
    "U.S. Representative, Dist II": "HI-02",
}
METHODS = ("mail", "in_person")
FIELDS, load_contract, database_row = with_methods(METHODS)
PARTIES = {"D": "DEM", "R": "REP", "G": "GRN", "L": "LIB"}


def integer(value, label):
    if not re.fullmatch(r"0|[1-9][0-9]{0,11}", value):
        raise ValueError(f"Invalid Hawaii {label}: {value!r}")
    return int(value)


def candidate(value, race):
    lines = [" ".join(line.split()) for line in value.splitlines() if line.strip()]
    match = re.fullmatch(r"\(([A-Z])\) (.+)", lines[0]) if lines else None
    if not match:
        raise ValueError(f"{race}: unexpected Hawaii candidate name")
    if race == "HI-GOV":
        if len(lines) != 4 or lines[1] != "For GOVERNOR" or lines[3] != "For LIEUTENANT GOVERNOR":
            raise ValueError("HI-GOV: unexpected Hawaii ticket layout")
        return f"{match[2]} / {lines[2]}", match[1]
    if len(lines) != 1:
        raise ValueError(f"{race}: unexpected Hawaii candidate name")
    return match[2], match[1]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "HI":
        raise ValueError("Unsupported Hawaii source/election")
    wanted = state_adapter("HI").races
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename).decode("utf-8-sig")
    if "\x00" in raw:
        raise ValueError("Hawaii summary contains NUL bytes")
    reader = csv.reader(io.StringIO(raw, newline=""), delimiter="\t", strict=True)
    first, header = next(reader, []), next(reader, [])
    if first[:1] != ["#FormatVersion 1"] or any(first[1:]) or header != HEADER:
        raise ValueError("Unexpected Hawaii summary layout")
    rows, contests, seen = [], {}, set()
    for values in reader:
        if len(values) != len(HEADER):
            raise ValueError("Unexpected Hawaii field count")
        record = dict(zip(HEADER, values))
        race = TITLES.get(" ".join(record["Contest Title"].split()))
        if race not in wanted:
            continue
        status = (record["#Contest ID"], record["Total Precincts"], record["Counted Precincts"])
        if contests.setdefault(race, status) != status or record["Contest Type"] != "OF":
            raise ValueError(f"{race}: inconsistent Hawaii contest rows")
        identity = text(record["Candidate ID"], "candidate ID")
        if (race, identity) in seen:
            raise ValueError(f"{race}: duplicate Hawaii candidate")
        seen.add((race, identity))
        name, letter = candidate(record["Candidate Name"], race)
        done = integer(record["Counted Precincts"], "precinct count")
        total = integer(record["Total Precincts"], "precinct count")
        if done > total:
            raise ValueError(f"{race}: more precincts counted than exist")
        rows.append(
            {
                **row(
                    race,
                    record["#Contest ID"],
                    identity,
                    name,
                    PARTIES.get(letter, letter),
                    integer(record["Total Votes"], "vote count"),
                    reporting=done,
                    total=total,
                ),
                "mail": integer(record["Mail Votes"], "vote count"),
                "in_person": integer(record["In-Person Votes"], "vote count"),
            }
        )
    for race in wanted:
        verify_scope(
            "HI", race, [r["source_candidate_id"] for r in rows if r["contest_id"] == race]
        )
    finish(rows, source, settings, output, stats, reference=reference, methods=METHODS)
    stats.update(contest_total_reconciliation="not_available", vote_methods="stored")
