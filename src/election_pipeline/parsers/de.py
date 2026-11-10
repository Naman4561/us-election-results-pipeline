"""Delaware Department of Elections results: totals by county.

A bundle holds the two documents the official "Unofficial Results" page loads for the
2026 general election (``GE2026``): ``statewide.json`` (every candidate's statewide
total, by vote method, with its candidate ID and the results' status) and
``counties.json`` (each candidate's count in Wilmington, the rest of New Castle, New
Castle as a whole, Kent and Sussex). Wilmington and the rest of New Castle must add up
to New Castle, the three counties to the state, and the state to the statewide file;
machine, absentee and early votes must add up to each statewide total. Counts are kept
by county; the vote-method split is checked but not stored, as counties have none.

Delaware elects a senator and its at-large U.S. House member in 2026; no governor.
"""

import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, formatted_count, row, text, unique_json

ELECTION = "GE2026"
RACES = {"U.S. Senator": "DE-SEN", "Representative In Congress": "DE-AL"}
STATUS = {"UNOFFICIAL RESULTS": False, "OFFICIAL RESULTS": True}
# The parties on Delaware's 2026 general election results; any other is refused.
PARTIES = {
    "Democratic Party": "DEM",
    "Republican Party": "REP",
    "Independent Party of Delaware": "IPD",
}
COUNTIES = ("New Castle", "Kent", "Sussex")


def records(raw, label):
    value = unique_json(raw.decode("utf-8-sig"))
    if not isinstance(value, list) or not value or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"Unexpected Delaware {label} document")
    return value


def party(name):
    name = text(name, "party")
    if name not in PARTIES:
        raise ValueError(f"Delaware party {name!r} is not in the reviewed list")
    return PARTIES[name]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "DE":
        raise ValueError("Unsupported Delaware source/election")
    wanted = state_adapter("DE").races
    with zipfile.ZipFile(archive_path) as archive:
        statewide = records(archive.read("statewide.json"), "statewide")
        by_county = records(archive.read("counties.json"), "county")
    statuses, times, candidates = set(), set(), {}
    for record in statewide:
        if (
            record.get("Election Id") != ELECTION
            or record.get("Election Date") != "2026-11-03"
            or record.get("Election Name") != "2026 General Election"
        ):
            raise ValueError("Delaware results belong to another election")
        statuses.add(record.get("Results Type"))
        times.add(record.get("ReportTime"))
        race = RACES.get(" ".join(text(record.get("Contest Title"), "contest").split()))
        if race not in wanted:
            continue
        name = " ".join(text(record.get("Candidate Name"), "candidate").split())
        key = (race, name)
        if key in candidates:
            raise ValueError(f"{race}: duplicate Delaware candidate")
        methods = [
            formatted_count(record.get(field))
            for field in ("Machine Votes", "Absentee Votes", "Early Voting Votes")
        ]
        total = formatted_count(record.get("Total Votes"))
        if sum(methods) != total:
            raise ValueError(f"{race} {name}: vote methods do not add up to the total")
        done, precincts = (
            count(record.get("Precincts Reported")),
            count(record.get("Total Precincts")),
        )
        if done > precincts:
            raise ValueError(f"{race}: more precincts reported than exist")
        candidates[key] = (
            str(count(record.get("Candidate Id"))),
            party(record.get("Party Name")),
            total,
            done,
            precincts,
        )
    if len(statuses) != 1 or statuses - set(STATUS):
        raise ValueError("Unexpected Delaware results status")
    if len(times) != 1:
        raise ValueError("Delaware results carry different report times")
    if {race for race, _ in candidates} != set(wanted):
        raise ValueError("Delaware results lack a selected contest")
    rows, seen = [], set()
    for record in by_county:
        race = RACES.get(" ".join(text(record.get("Contest Title"), "contest").split()))
        if race not in wanted:
            continue
        name = " ".join(text(record.get("Candidate Name"), "candidate").split())
        key = (race, name)
        if key not in candidates or key in seen:
            raise ValueError(f"{race} {name}: unexpected or duplicate county row")
        seen.add(key)
        identity, code, total, done, precincts = candidates[key]
        if party(record.get("Party Name")) != code or record.get("Results Type") not in STATUS:
            raise ValueError(f"{race} {name}: county row differs from the statewide file")
        votes = {place: count(record.get(place)) for place in COUNTIES}
        if (
            count(record.get("Wilmington")) + count(record.get("Rest of New Castle"))
            != votes["New Castle"]
            or sum(votes.values()) != count(record.get("State"))
            or count(record.get("State")) != total
        ):
            raise ValueError(f"{race} {name}: county counts do not add up")
        rows.append(row(race, race, identity, name, code, total, reporting=done, total=precincts))
        for place, value in votes.items():
            rows.append(row(race, race, identity, name, code, value, county=place))
    if seen != set(candidates):
        raise ValueError("Delaware county results lack a candidate")
    for race in wanted:
        verify_scope(
            "DE", race, [candidates[k][0] for k in candidates if k[0] == race], list(COUNTIES)
        )
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=text(times.pop(), "report time"),
        official_results=STATUS[statuses.pop()],
        contest_total_reconciliation="passed",
        vote_methods="reported_not_stored",
    )
