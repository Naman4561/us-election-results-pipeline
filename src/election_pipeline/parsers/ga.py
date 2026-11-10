"""Georgia's official Enhanced Voting JSON; one aggregate response per check."""

import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, party, row, text, unique_json


def english(values):
    matches = [x["text"] for x in values if x.get("languageId") == "en"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one English value")
    return text(matches[0])


def race_name(name):
    if name in {"US Senate", "Governor"}:
        return {"US Senate": "GA-SEN", "Governor": "GA-GOV"}[name]
    match = re.fullmatch(r"US House of Representatives - District (\d+)", name)
    return f"GA-{int(match[1]):02}" if match else None


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported Georgia source/election")
    with zipfile.ZipFile(archive_path) as archive:
        data = unique_json(archive.read(source.primary_filename))
    election = data["election"]
    if (
        election["electionDate"] != source.election_date
        or election["isPrimary"] is not False
        or election["isProduction"] is not True
        or data["jurisdiction"]["shortName"] != "Georgia"
    ):
        raise ValueError("Wrong Georgia election or jurisdiction")
    from election_pipeline.states import state_adapter

    rows, seen = [], set()
    for ballot in data["ballotItems"]:
        race = race_name(english(ballot["name"]))
        if race not in state_adapter("GA").races:
            continue
        if (
            race in seen
            or ballot["contestType"] != "Candidate"
            or english(ballot["voteFor"]) != "Vote for 1"
            or ballot["parentId"] is not None
            or ballot["publishPublicElectionId"] != "11032026GeneralAndSpecialElection"
        ):
            raise ValueError("Unexpected or duplicate Georgia race")
        seen.add(race)
        options = ballot["summaryResults"]["ballotOptions"]
        verify_scope("GA", race, [c["id"] for c in options])
        if sum(count(c["voteCount"]) for c in options) != count(ballot["voteTotal"]):
            raise ValueError("Georgia candidate sum disagrees with race total")
        for candidate in options:
            groups = candidate["groupResults"]
            if groups:
                ids = [g["groupId"] for g in groups]
                if len(ids) != len(set(ids)) or sum(count(g["voteCount"]) for g in groups) != count(
                    candidate["voteCount"]
                ):
                    raise ValueError("Georgia vote-method totals disagree")
            abbreviation = (candidate.get("party") or {}).get("abbreviation") or ""
            rows.append(
                row(
                    race,
                    ballot["id"],
                    candidate["id"],
                    english(candidate["name"]),
                    party(abbreviation),
                    candidate["voteCount"],
                )
            )
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        source_version=text(election["publishVersionId"]),
        source_update_text=text(election["asOf"]),
        official_results=election.get("isOfficialResults") is True,
        contest_total_reconciliation="passed",
    )
