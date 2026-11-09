"""Ohio's published IEEE/NIST 1622 XML, preserving one row per ticket selection."""

import re
import zipfile

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import party, row, text, xml
from election_pipeline.states import state_adapter


def indexed(elements):
    result = {}
    for element in elements:
        identity = text(element.get("ObjectId"), "XML object ID")
        if identity in result:
            raise ValueError("Duplicate Ohio XML object ID")
        result[identity] = element
    return result


def english(element):
    if element is None:
        raise ValueError("Missing Ohio name element")
    values = element.findall("LanguageString")
    matches = [n.text for n in values if n.get("Language") == "en"]
    if len(matches) != 1:
        raise ValueError("Missing/duplicate Ohio English name")
    return text(matches[0])


def race_name(name):
    if name in {"U.S. Senator", "Governor and Lieutenant Governor"}:
        return {"U.S. Senator": "OH-SEN", "Governor and Lieutenant Governor": "OH-GOV"}[name]
    match = re.fullmatch(r"Representative to Congress - District (\d{2})", name)
    return f"OH-{int(match[1]):02}" if match else None


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported Ohio source/election")
    with zipfile.ZipFile(archive_path) as archive:
        root = xml(archive.read(source.primary_filename))
    elections = root.findall("Election")
    if (
        root.tag != "ElectionReport"
        or root.get("IssuerAbbreviation") != "OH"
        or root.get("Format") != "SummaryContest"
        or len(elections) != 1
        or elections[0].get("Date") != source.election_date
        or elections[0].get("Type") != "General"
    ):
        raise ValueError("Wrong Ohio election/report")
    units = indexed(root.findall("GpUnitCollection/GpUnit"))
    candidates = indexed(elections[0].findall("CandidateCollection/Candidate"))
    parties = indexed(root.findall("PartyCollection/Party"))
    county_units = {
        k: u
        for k, u in units.items()
        if u.get("Type") == "County" and u.get("IsElectoralDistrict") == "false"
    }
    if len(county_units) != 88:
        raise ValueError("Ohio must identify all 88 counties")
    rows, seen = [], set()
    for contest in elections[0].findall("ContestCollection/Contest"):
        race = race_name(contest.get("Name", ""))
        if race not in state_adapter("OH").races:
            continue
        if (
            race in seen
            or contest.get("VotesAllowed") != "1"
            or contest.get("NumberElected") != "1"
        ):
            raise ValueError("Duplicate/multi-selection Ohio contest")
        seen.add(race)
        scope = text(contest.findtext("JurisdictionalScopeId"))
        if units[scope].get("Type") != ("Congressional" if race[3:].isdigit() else "State"):
            raise ValueError("Ohio contest has wrong jurisdictional scope")
        coverage = None
        for selection in contest.findall("BallotSelection"):
            refs = selection.findall("CandidateId")
            if not refs:
                raise ValueError("Missing Ohio selection candidate")
            people = [candidates[text(c.text)] for c in refs]
            top = [c for c in people if c.get("IsTopTicket") != "false"]
            if len(top) != 1:
                raise ValueError("Ohio ticket requires exactly one leading candidate")
            chosen = top[0]
            affiliations = [text(c.findtext("PartyId")) for c in people]
            if len(set(affiliations)) != 1:
                raise ValueError("Ohio ticket parties disagree")
            affiliation = party(parties[affiliations[0]].get("Abbreviation", ""))
            name = english(chosen.find("BallotName"))
            cells, counties = {}, set()
            for vote in selection.findall("VoteCountsCollection/VoteCounts"):
                if vote.get("Type") != "Total":
                    raise ValueError("Unexpected Ohio vote type; refusing possible double counting")
                unit = text(vote.findtext("GpUnitId"))
                if unit in cells:
                    raise ValueError("Duplicate Ohio geographic vote cell")
                cells[unit] = integer(text(vote.get("Count")), "Ohio votes")
                if unit != scope:
                    if unit not in county_units:
                        raise ValueError("Unexpected Ohio vote geography")
                    counties.add(text(units[unit].get("Name"), "Ohio county name").upper())
                rows.append(
                    row(
                        race,
                        contest.get("ObjectId"),
                        selection.get("ObjectId"),
                        name,
                        affiliation,
                        cells[unit],
                        county=""
                        if unit == scope
                        else text(units[unit].get("Name"), "Ohio county name").upper(),
                    )
                )
            if (
                scope not in cells
                or not counties
                or (race in {"OH-SEN", "OH-GOV"} and len(counties) != 88)
            ):
                raise ValueError("Missing Ohio official total or county coverage")
            if coverage is not None and coverage != counties:
                raise ValueError("Ohio candidate county coverage differs")
            coverage = counties
        verify_scope(
            "OH", race, [s.get("ObjectId") for s in contest.findall("BallotSelection")], coverage
        )
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=text(root.get("GeneratedDate")),
        source_status=text(root.get("Status")),
        contest_total_reconciliation="not_available",
    )
