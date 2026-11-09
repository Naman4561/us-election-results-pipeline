"""Kentucky ENR v6: keep every reporting level; reconcile county and state totals."""

import re
import zipfile
from datetime import date
from xml.etree import ElementTree as ET


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("Kentucky count or identifier is not a nonnegative integer")
    return int(value)


def xml_root(archive, name):
    data = archive.read(name)
    text = data.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML declarations for DTDs/entities are forbidden")
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError("Malformed Kentucky XML") from error


def values(element):
    result = {}
    for field in element:
        key = field.tag.rsplit("}", 1)[-1]
        if key in result:
            raise ValueError("Duplicate XML field")
        result[key] = field.text
    return result


def indexed(root, key):
    result = {}
    for element in root:
        row = values(element)
        identity = row[key]
        integer(identity)
        if identity in result:
            raise ValueError(f"Duplicate Kentucky {key}")
        result[identity] = row
    return result


def contest_key(name):
    if name.upper() == "UNITED STATES SENATOR":
        return "KY-SEN"
    match = re.fullmatch(
        r"UNITED STATES REPRESENTATIVE in CONGRESS ([1-6])(?:st|nd|rd|th) Congressional District",
        name,
        re.IGNORECASE,
    )
    return f"KY-{int(match[1]):02}" if match else None


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.parsers.results import finish
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.dataset != "election_results":
        raise ValueError("Kentucky election results require a pinned XML bundle")
    with zipfile.ZipFile(archive_path) as archive:
        election = values(xml_root(archive, "election.xml"))
        if (
            date.fromisoformat(election["ElectionDate"][:10]).isoformat() != source.election_date
            or election["ElectionType"] != "General"
        ):
            raise ValueError("Kentucky bundle belongs to a different election")
        parties = indexed(xml_root(archive, "parties.xml"), "PartyId")
        units = indexed(xml_root(archive, "units.xml"), "UnitId")
        candidates = indexed(xml_root(archive, "candidates.xml"), "CandidateId")
        contests = indexed(xml_root(archive, "contests.xml"), "ContestId")
        selected = {k: contest_key(v["ContestName"]) for k, v in contests.items()}
        selected = {k: v for k, v in selected.items() if v}
        if set(selected.values()) != set(state_adapter("KY").races) or len(selected) != len(
            state_adapter("KY").races
        ):
            raise ValueError("Kentucky federal contest coverage is incomplete or duplicated")

        def geography(identity):
            unit = units[identity]
            kind = unit["UnitType"]
            county, precinct = "", ""
            if kind == "County":
                county = unit["UnitName"]
            elif kind == "Precinct":
                parent = units[unit["ParentId"]]
                if parent["UnitType"] != "County":
                    raise ValueError("Precinct lacks a county parent")
                county, precinct = parent["UnitName"], unit["UnitName"]
            elif kind != "State":
                raise ValueError("Unsupported federal reporting level")
            county = re.sub(r"\s+County$", "", county, flags=re.IGNORECASE).upper()
            return kind.lower(), county, precinct

        root = xml_root(archive, "results.xml")
        sections = {element.tag.rsplit("}", 1)[-1]: element for element in root}
        rows, result_keys = [], set()
        for element in sections["CandidateData"]:
            raw = values(element)
            if raw["ContestId"] not in selected:
                continue
            candidate = candidates[raw["CandidateId"]]
            if candidate["ContestId"] != raw["ContestId"]:
                raise ValueError("Candidate belongs to a different contest")
            kind, name, precinct = geography(raw["ReportingUnitId"])
            party = candidate["PoliticalPartyId"]
            if party != "0" and party not in parties:
                raise ValueError("Unknown candidate party")
            day, total = integer(raw["ElectionDayVotes"]), integer(raw["TotalVotes"])
            if day > total:
                raise ValueError("Kentucky Election Day votes exceed total votes")
            key = (raw["ReportingUnitId"], raw["ContestId"], raw["CandidateId"])
            if key in result_keys:
                raise ValueError("Duplicate Kentucky result key")
            result_keys.add(key)
            if len(rows) >= settings.max_csv_rows:
                raise ValueError("Kentucky results exceed the row budget")
            rows.append(
                dict(
                    unit_type=kind,
                    county=name,
                    precinct=precinct,
                    contest_id=selected[raw["ContestId"]],
                    source_contest_id=raw["ContestId"],
                    source_candidate_id=raw["CandidateId"],
                    choice=candidate["BallotName"],
                    choice_party=parties[party]["Abbreviation"] if party != "0" else "",
                    total_votes=total,
                    precincts_reporting=None,
                    precincts_total=None,
                    election_day=day,
                    absentee_combined=total - day,
                    attributes={"reporting_unit_id": raw["ReportingUnitId"]},
                )
            )
        senate = {
            r["county"] for r in rows if r["unit_type"] == "county" and r["contest_id"] == "KY-SEN"
        }
        if len(senate) != 120:
            raise ValueError("Kentucky county/race coverage is incomplete")

        reporting, seen = [], set()
        for element in sections["ReportData"]:
            raw = values(element)
            identity = raw["ReportingUnitId"]
            if identity in seen or len(reporting) >= settings.max_csv_rows:
                raise ValueError("Duplicate/excessive Kentucky reporting units")
            seen.add(identity)
            kind, name, precinct = geography(identity)
            status = raw["ReportingStatus"]
            if status not in {"NotReporting", "PartialReporting", "FinalReporting"}:
                raise ValueError("Unknown Kentucky reporting status")
            numbers = [
                integer(raw[k])
                for k in (
                    "PrecinctsParticipating",
                    "PrecinctsReporting",
                    "BallotsCast",
                    "RegisteredVoters",
                )
            ]
            if numbers[1] > numbers[0]:
                raise ValueError("Reporting precinct count exceeds participating count")
            reporting.append(
                dict(
                    unit_type=kind,
                    county=name,
                    precinct=precinct,
                    source_unit_id=identity,
                    reporting_status=status,
                    precincts_participating=numbers[0],
                    precincts_reporting=numbers[1],
                    ballots_cast=numbers[2],
                    registered_voters=numbers[3],
                )
            )
        if {key[0] for key in result_keys} - seen:
            raise ValueError("Candidate rows lack reporting-unit metadata")
    finish(
        rows, source, settings, output, stats, reference=reference, reconcile=True, units=reporting
    )
    stats["warnings"].append(
        "Absentee is combined; early/mail/provisional breakdowns are unavailable."
    )
