"""Texas SOS Civix statewide totals from one versioned JSON response."""

import base64
import json
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit

from election_pipeline.parsers.results import finish


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate Texas JSON key: {key}")
        value[key] = item
    return value


def read_json(raw):
    def invalid_constant(value):
        raise ValueError(f"Invalid Texas JSON constant: {value}")

    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Texas JSON must contain an object")
    return value


def section(envelope, name):
    value = envelope.get(name)
    if not isinstance(value, str):
        raise ValueError(f"Missing encoded Texas section: {name}")
    try:
        return read_json(base64.b64decode(value, validate=True))
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid encoded Texas section: {name}") from error


def count(value, label):
    if type(value) is not int or not 0 <= value <= 9223372036854775807:
        raise ValueError(f"Invalid Texas {label}: expected a nonnegative integer")
    return value


def contest_id(name):
    if not isinstance(name, str):
        raise ValueError("Invalid Texas race name")
    normalized = re.sub(r"[^A-Z0-9]", "", name.upper())
    if normalized in ("USSENATOR", "UNITEDSTATESSENATOR"):
        return "TX-SEN"
    if normalized == "GOVERNOR":
        return "TX-GOV"
    match = re.fullmatch(r"(?:US|UNITEDSTATES)REPRESENTATIVEDISTRICT([0-9]+)", normalized)
    return f"TX-{int(match[1]):02}" if match else None


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    expected_id = re.fullmatch(
        r"/api-ivis-system/api/s3/enr/election/([0-9]+)", urlsplit(source.url).path
    )
    if source.state != "TX" or not expected_id or source.primary_filename != "results.json":
        raise ValueError("Unsupported Texas Civix source")
    with zipfile.ZipFile(archive_path) as archive:
        envelope = read_json(archive.read(source.primary_filename))
    version = envelope.get("Version")
    if not isinstance(version, str) or not re.fullmatch(
        "enr/" + expected_id[1] + r"/[0-9]+/", version
    ):
        raise ValueError("Texas version belongs to another election or is malformed")
    home = section(envelope, "Home")
    raw_date = home.get("ElecDate")
    if not isinstance(raw_date, str) or not re.fullmatch(r"[0-9]{8}", raw_date):
        raise ValueError("Invalid Texas election date")
    election_date = datetime.strptime(raw_date, "%m%d%Y").date().isoformat()
    if election_date != source.election_date:
        raise ValueError("Texas Home section belongs to another election date")
    updated = home.get("LastUpdatedTime")
    if not isinstance(updated, str):
        raise ValueError("Missing Texas source update text")
    datetime.strptime(updated, "%b %d, %Y %H:%M:%S")

    wanted, selected, rows = state_adapter("TX").races, set(), []
    for name, office in (("Federal", "FEDERAL OFFICES"), ("StateWide", "STATEWIDE OFFICES")):
        block = section(envelope, name)
        races = block.get("Races")
        if block.get("OffType") != office or not isinstance(races, list):
            raise ValueError(f"Invalid Texas {name} race list")
        for race in races:
            if not isinstance(race, dict):
                raise ValueError("Invalid Texas race object")
            identity = contest_id(race.get("N"))
            if identity not in wanted:
                continue
            if identity in selected or (identity == "TX-GOV") != (name == "StateWide"):
                raise ValueError("Duplicate Texas race or race in the wrong section")
            selected.add(identity)
            race_id = count(race.get("id"), "race ID")
            if not race_id:
                raise ValueError("Missing Texas race ID")
            candidates = race.get("Candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("Incomplete Texas candidate list")
            total = 0
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise ValueError("Invalid Texas candidate object")
                candidate_id = count(candidate.get("ID"), "candidate ID")
                if not candidate_id:
                    raise ValueError("Missing Texas candidate ID")
                choice, party = candidate.get("N"), candidate.get("P")
                if (
                    not isinstance(choice, str)
                    or not choice.strip()
                    or not isinstance(party, str)
                    or not re.fullmatch(r"[A-Z]{1,8}", party)
                ):
                    raise ValueError("Missing Texas candidate name or party")
                votes = count(candidate.get("V"), "candidate votes")
                total += votes
                rows.append(
                    dict(
                        unit_type="state",
                        county="",
                        contest_id=identity,
                        source_contest_id=str(race_id),
                        source_candidate_id=str(candidate_id),
                        choice=choice.strip(),
                        choice_party=party,
                        total_votes=votes,
                        precincts_reporting=None,
                        precincts_total=None,
                    )
                )
            if count(race.get("T"), "contest total") != total:
                raise ValueError(
                    f"Texas candidate votes do not equal the reported total: {identity}"
                )
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        source_version=version,
        source_update_text=updated,
        source_update_timezone="not specified by source; no timezone inferred",
        contest_total_reconciliation="passed",
    )
