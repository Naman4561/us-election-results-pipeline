"""California Secretary of State results API: statewide (and districtwide) totals.

``governor.json`` and ``us-rep-NN.json`` are the official API documents
(``api.sos.ca.gov/returns/...``) behind the Secretary of State's results site, one per
contest. California elects no U.S. senator in 2026. County detail is not collected: the
API serves it only as one more document per county and contest, which would multiply
each collection several times over.

The API gives candidates no IDs, so a candidate's ID is its published name; the reviewed
roster pins every name. Counts are printed with thousands separators and must parse
exactly. Each document's "precincts reporting" line is stored with its statewide rows.
"""

import re
import zipfile

from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import formatted_count, row, text, unique_json
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

PARTIES = {"Dem": "DEM", "Rep": "REP", "Lib": "LIB", "Grn": "GRN"}
REPORTING = re.compile(
    r"[0-9]{1,3}(?:\.[0-9])?% \(([0-9][0-9,]*) of ([0-9][0-9,]*)\) precincts reporting"
)
TIME = re.compile(r"[A-Z][a-z]+ [0-9]{1,2}, 2026, [0-9]{1,2}:[0-9]{2} [ap]\.m\.")


def member(race):
    return "governor.json" if race == "CA-GOV" else f"us-rep-{race[3:]}.json"


def title(race):
    if race == "CA-GOV":
        return "Governor - Statewide Results"
    return f"U.S. House of Representatives District {int(race[3:])} - Districtwide Results"


def document(raw, race):
    data = unique_json(raw)
    if not isinstance(data, dict) or set(data) != {
        "raceTitle",
        "Reporting",
        "ReportingTime",
        "candidates",
    }:
        raise ValueError(f"{race}: unexpected California API document")
    if text(data["raceTitle"], "race title") != title(race):
        raise ValueError(f"{race}: California API document belongs to another contest")
    reporting = REPORTING.fullmatch(text(data["Reporting"], "reporting"))
    updated = text(data["ReportingTime"], "reporting time")
    if not reporting or not TIME.fullmatch(updated):
        raise ValueError(f"{race}: unexpected California reporting status")
    done, total = (formatted_count(v) for v in reporting.groups())
    if done > total or not isinstance(data["candidates"], list) or not data["candidates"]:
        raise ValueError(f"{race}: invalid California reporting status or candidates")
    return data["candidates"], done, total, updated


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "CA":
        raise ValueError("Unsupported California source/election")
    wanted = state_adapter("CA").selected_races(source)
    rows, times = [], set()
    with zipfile.ZipFile(archive_path) as archive:
        for race in wanted:
            candidates, done, total, updated = document(archive.read(member(race)), race)
            times.add(updated)
            names = []
            for candidate in candidates:
                if not isinstance(candidate, dict) or set(candidate) != {
                    "Name",
                    "Party",
                    "Votes",
                    "Percent",
                    "incumbent",
                }:
                    raise ValueError(f"{race}: unexpected California candidate record")
                name = text(candidate["Name"], "candidate")
                label = text(candidate["Party"], "party")
                names.append(name)
                rows.append(
                    row(
                        race,
                        race,
                        name,
                        name,
                        PARTIES.get(label, label.upper()),
                        formatted_count(candidate["Votes"]),
                        reporting=done,
                        total=total,
                    )
                )
            verify_scope("CA", race, names)
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        source_update_text="; ".join(sorted(times)),
        contest_total_reconciliation="not_available",
        vote_methods="not_published",
    )
