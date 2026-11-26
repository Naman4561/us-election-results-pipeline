"""Connecticut Secretary of the State results: the official results app's data files.

One version of the app's files (``collectors.connecticut``): ``lookup.json`` names the
election, its 169 towns, offices, parties and candidate lines; ``state.json`` holds
each office's statewide votes by candidate line and ``towns.json`` the same by town;
``status.json`` says whether each town's results are unofficial and how many of its
polling places reported; ``election.json`` the statewide polling places reported.
Connecticut reports by town; it has no county results.

A candidate can run on several party lines (fusion): the lines are added up under the
candidate's major-party line, and write-in candidates are one "Write-in" line. Every
line's town votes must add up to its statewide votes. The app prints no update time of
the state's own, so the version number is recorded instead.
"""

import re
import zipfile

from election_pipeline import parse_report
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, unique_json

ELECTION = ("108", "11/03/2026", "2026 General Election")
GOVERNOR = "Governor and Lieutenant Governor"
HOUSE = re.compile(r"Representative in Congress ([1-5])")
# The parties of these offices' candidate lines; any other is refused.
PARTIES = {"D": "DEM", "R": "REP", "WF": "WFP", "IND": "IND", "G": "GRN", "LI": "LIB"}
MAJOR = ("DEM", "REP")
WRITE_IN_PARTY = "WI"
WRITE_IN = ("write-in", "Write-in")
REPORTED = re.compile(r"([0-9]+) of ([0-9]+) \([0-9.]+%\)")
STATUS = {"( *Unofficial Results )": False, "( *Official Results )": True}


def votes(value, label):
    text = str(value).replace(",", "")
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise ValueError(f"Connecticut results: invalid {label} {value!r}")
    return count(int(text))


def lines(entries, label):
    """{candidate line ID: votes} from an office's list of single-key entries."""
    found = {}
    for entry in entries:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(f"{label}: unexpected Connecticut vote entry")
        ((line, value),) = entry.items()
        if line in found:
            raise ValueError(f"{label}: repeated Connecticut candidate line")
        found[line] = votes(value["V"], label)
    return found


def reported(text, label):
    match = REPORTED.fullmatch(str(text).strip())
    if not match or int(match[1]) > int(match[2]):
        raise ValueError(f"{label}: unexpected Connecticut reporting count {text!r}")
    return int(match[1]), int(match[2])


def races(lookup, wanted):
    """{race: office ID} for the selected offices."""
    found = {}
    for entry in lookup["officeList"]:
        ((identity, office),) = entry.items()
        name = office["NM"]
        if name == GOVERNOR and office["OT"] == "SW":
            race = "CT-GOV"
        elif (match := HOUSE.fullmatch(name)) and office["OT"] == "C":
            if office["D"] != match[1]:
                raise ValueError(f"{name}: district does not match its office")
            race = f"CT-{int(match[1]):02}"
        else:
            continue
        if race not in wanted or race in found or office["ID"] != identity:
            raise ValueError(f"Unexpected or repeated Connecticut office {name!r}")
        found[race] = identity
    if set(found) != set(wanted):
        raise ValueError("Connecticut results lack a selected office")
    return found


def choices(lookup, line_ids, race):
    """{line: (name, party)} for one office's candidate lines: each candidate under its
    major-party line (or its only party), every write-in line as one Write-in choice."""
    found = {}
    for line in line_ids:
        record = lookup["candidateIds"].get(line) or {}
        party = lookup["partyIds"].get(str(record.get("P")), {}).get("CD")
        name = " ".join(str(record.get("NM", "")).split())
        if not name or party is None:
            raise ValueError(f"{race}: Connecticut candidate line {line} is unknown")
        if party != WRITE_IN_PARTY and party not in PARTIES:
            raise ValueError(f"{race}: unexpected Connecticut party {party!r}")
        found[line] = (name, None if party == WRITE_IN_PARTY else PARTIES[party])
    main = {}
    for name, party in found.values():
        if party is not None and main.get(name) not in MAJOR:
            main[name] = party
    return {
        line: (WRITE_IN[1], "") if party is None else (name, main[name])
        for line, (name, party) in found.items()
    }


def added(line_votes, names):
    """{(name, party): votes}, each choice's lines added up."""
    total = {}
    for line, value in line_votes.items():
        total[names[line]] = total.get(names[line], 0) + value
    return total


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "CT":
        raise ValueError("Unsupported Connecticut source/election")
    with zipfile.ZipFile(archive_path) as archive:
        number = unique_json(archive.read(source.primary_filename))["Version"]
        lookup, election, statewide, by_town, status = (
            unique_json(archive.read(name))
            for name in ("lookup.json", "election.json", "state.json", "towns.json", "status.json")
        )
    record = lookup["election"]
    if (record["ID"], record["DT"], record["DNM"]) != ELECTION or election["ID"] != ELECTION[0]:
        raise ValueError("Connecticut results belong to another election")
    towns = {str(k): " ".join(str(v).split()) for k, v in lookup["townIds"].items()}
    if len(towns) != 169 or len(set(towns.values())) != 169 or set(by_town) != set(towns):
        raise ValueError("Connecticut results lack their 169 towns")
    if set(status) != set(towns) or {s["TS"] for s in status.values()} - STATUS.keys():
        raise ValueError("Connecticut town status is missing or unexpected")
    official = {STATUS[s["TS"]] for s in status.values()}
    done, total = reported(election["PR"], "statewide")
    rows = []
    for race, office in races(lookup, state_adapter("CT").races).items():
        state_lines = lines(statewide[office], race)
        names = choices(lookup, state_lines, race)
        sums, places = dict.fromkeys(state_lines, 0), []
        for town_id, town in sorted(towns.items(), key=lambda t: t[1]):
            if office not in by_town[town_id]:
                continue
            local = lines(by_town[town_id][office], f"{race} {town}")
            if not local.keys() <= state_lines.keys():
                raise ValueError(f"{race} {town}: candidate line missing statewide")
            places.append(town)
            here, of = reported(status[town_id]["PR"], town)
            for line, value in local.items():
                sums[line] += value
            for (name, party), value in added(local, names).items():
                key = name if party else WRITE_IN[0]
                rows.append(
                    row(race, office, key, name, party, value, town, reporting=here, total=of)
                )
        if sums != state_lines:
            raise ValueError(f"{race}: Connecticut towns do not add up to the statewide votes")
        for (name, party), value in added(state_lines, names).items():
            key = name if party else WRITE_IN[0]
            rows.append(row(race, office, key, name, party, value, reporting=done, total=total))
        verify_scope("CT", race, sorted({n for n, p in names.values() if p}), places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=f"version {number}",
        **parse_report.fields(official == {True}),
        contest_total_reconciliation="passed",
        party_line_reconciliation="passed",
    )
