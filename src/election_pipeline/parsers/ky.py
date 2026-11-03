"""Kentucky ENR v6: preserve reporting levels; reconcile county and state totals."""

import csv
import re
import zipfile
from collections import defaultdict
from datetime import date
from xml.etree import ElementTree as ET

FIELDS = (
    "source_line",
    "election_date",
    "state",
    "reporting_unit_id",
    "unit_type",
    "county",
    "precinct",
    "contest_id",
    "choice",
    "choice_party",
    "election_day",
    "absentee_combined",
    "total_votes",
)
REPORT_FIELDS = (
    "source_line",
    "election_date",
    "state",
    "reporting_unit_id",
    "unit_type",
    "county",
    "precinct",
    "reporting_status",
    "precincts_participating",
    "precincts_reporting",
    "ballots_cast",
    "registered_voters",
)
VOTES = ("election_day", "absentee_combined", "total_votes")
RACES = {"KY-SEN": "U.S. Senate", **{f"KY-{n:02}": f"U.S. House · KY-{n:02}" for n in range(1, 7)}}


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
    from election_pipeline.parsing import reference_check, write_groups

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
        if set(selected.values()) != set(RACES) or len(selected) != len(RACES):
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
        official, county, result_keys = {}, {}, set()
        coverage, totals = defaultdict(set), [0, 0, 0]
        path = output / "reporting_results.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, FIELDS)
            writer.writeheader()
            count = 0
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
                count += 1
                if count > settings.max_csv_rows:
                    raise ValueError("Kentucky results exceed the row budget")
                row = dict(
                    zip(
                        FIELDS,
                        (
                            count + 1,
                            source.election_date,
                            source.state,
                            raw["ReportingUnitId"],
                            kind,
                            name,
                            precinct,
                            selected[raw["ContestId"]],
                            candidate["BallotName"],
                            parties[party]["Abbreviation"] if party != "0" else "",
                            day,
                            total - day,
                            total,
                        ),
                    )
                )
                writer.writerow(row)
                pair = (row["contest_id"], row["choice"], row["choice_party"])
                measures = [row[m] for m in VOTES]
                if kind == "state":
                    if pair in official:
                        raise ValueError("Duplicate statewide candidate total")
                    official[pair] = measures
                elif kind == "county":
                    key = (source.election_date, source.state, name, *pair)
                    if key in county:
                        raise ValueError("Duplicate county candidate total")
                    county[key] = measures
                    coverage[row["contest_id"]].add(name)
                    totals = [a + b for a, b in zip(totals, measures)]
        calculated = {}
        for key, measures in county.items():
            pair = key[3:]
            combined = calculated.setdefault(pair, [0, 0, 0])
            for i, value in enumerate(measures):
                combined[i] += value
        if calculated != official:
            raise ValueError("Kentucky county and statewide candidate totals disagree")
        if len(coverage["KY-SEN"]) != 120 or set(coverage) != set(RACES):
            raise ValueError("Kentucky county/race coverage is incomplete")
        write_groups(
            output / "county_results.csv",
            county,
            ("election_date", "state", "county", "contest_id", "choice", "choice_party"),
            VOTES,
        )
        state = {(source.election_date, source.state, *k): v for k, v in official.items()}
        write_groups(
            output / "contest_results.csv",
            state,
            ("election_date", "state", "contest_id", "choice", "choice_party"),
            VOTES,
        )

        with (output / "reporting_units.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, REPORT_FIELDS)
            writer.writeheader()
            seen = set()
            for index, element in enumerate(sections["ReportData"], 2):
                raw = values(element)
                identity = raw["ReportingUnitId"]
                if identity in seen or index > settings.max_csv_rows + 1:
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
                writer.writerow(
                    dict(
                        zip(
                            REPORT_FIELDS,
                            (
                                index,
                                source.election_date,
                                source.state,
                                identity,
                                kind,
                                name,
                                precinct,
                                status,
                                *numbers,
                            ),
                        )
                    )
                )
            if {key[0] for key in result_keys} - seen:
                raise ValueError("Candidate rows lack reporting-unit metadata")
    stats.update(
        output_rows=count,
        reporting_unit_rows=len(seen),
        internal_reconciliation="passed",
        all_counts_zero=not any(totals),
        reference_comparison=reference_check(reference, source, state, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided"},
        warnings=["Absentee is combined; early/mail/provisional breakdowns are unavailable."]
        + (["All counts are zero; live reporting is not established."] if not any(totals) else []),
        election_status="not determined by parser",
    )


def load_contract(dataset):
    from election_pipeline.states import LoadContract

    if dataset != "election_results":
        raise ValueError("Unsupported Kentucky dataset")
    return LoadContract(
        "results",
        "reporting_results.csv",
        FIELDS,
        (
            "reporting_results.csv",
            "reporting_units.csv",
            "county_results.csv",
            "contest_results.csv",
        ),
        "bundle",
        (("reporting_units", "reporting_units.csv", REPORT_FIELDS, "reporting_unit_rows"),),
    )


def database_row(row, source, release):
    from election_pipeline.database import number

    fields = REPORT_FIELDS if "reporting_status" in row else FIELDS
    numeric = {
        "source_line",
        "election_day",
        "absentee_combined",
        "total_votes",
        "precincts_participating",
        "precincts_reporting",
        "ballots_cast",
        "registered_voters",
    }
    return (
        release,
        *(
            number(row[k]) if k in numeric else (row[k] or None) if k == "choice_party" else row[k]
            for k in fields
            if k not in {"election_date", "state"}
        ),
    )
