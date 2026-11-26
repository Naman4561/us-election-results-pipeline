"""Nevada Secretary of State: the results app's data files, saved by hand.

The results site (silverstateelection.nv.gov) and its data files refuse automated
clients, so the operator saves, from the browser, the election's ``manifest.json``, the
``overview.json`` it names (counties, parties, every contest's choices and statewide
totals) and the contest results files it names for the selected contests (each
contest's votes by county), and imports them unchanged as one zip (``results.zip``;
contest files under their own SHA-256 names).

Every file must have the SHA-256 the manifest gives it; every county's votes must add up,
choice by choice, to the overview's statewide totals; and the counties received must be
the contest's counties. A county the state masks for voter privacy (no votes, lead
"Masked") is not in the statewide totals either and is left out of that contest. "None
of These Candidates" is a choice on Nevada's statewide ballots and is kept as one. The
manifest's generation time is the report time.
"""

import hashlib
import io
import re
import zipfile
from datetime import datetime

from election_pipeline import parse_report
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, unique_json

ELECTION = ("2026 General Election", "2026-11-03", "General")
GOVERNOR = "Governor"
HOUSE = re.compile(r"Representative In Congress, District ([1-4])")
NONE_OF_THESE = ("none-of-these", "None of These Candidates")
MAX_BYTES = 5_000_000
MAX_MEMBERS = 20
# The import zip's layout, for scripts/pack_import.py (election_pipeline.packing): the
# results app's files, which the parser checks against the app's own manifest.json.
PACK = {"members": None}


def race_name(name):
    if name == GOVERNOR:
        return "NV-GOV"
    match = HOUSE.fullmatch(name)
    return f"NV-0{match[1]}" if match else None


def member(bundle, name, sha256):
    """A member's bytes, which must have the SHA-256 the manifest gives it."""
    try:
        raw = bundle.read(name)
    except KeyError as error:
        raise ValueError(f"Nevada import lacks {name}") from error
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError(f"Nevada {name} differs from the manifest's copy")
    return raw


def display(label):
    """A ballot label ("Ford, Aaron D.") as the name is written ("Aaron D. Ford")."""
    parts = " ".join(str(label).split()).split(", ")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Unexpected Nevada candidate label {label!r}")
    return f"{parts[1]} {parts[0]}"


def votes(value, label):
    if type(value) is not int:
        raise ValueError(f"Nevada results: invalid {label} {value!r}")
    return count(value)


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "NV":
        raise ValueError("Unsupported Nevada source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_MEMBERS or any(
            e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries
        ):
            raise ValueError("Nevada import has too many, too large or encrypted files")
        manifest = unique_json(bundle.read("manifest.json"))
        if manifest.get("kind") != "Manifest" or manifest.get("schemaVersion") != 2:
            raise ValueError("Nevada import lacks its manifest")
        overview = unique_json(member(bundle, "overview.json", manifest["overview"]["sha256"]))
        files = {c["contestId"]: c["results"]["sha256"] for c in manifest["contests"]}
        election = overview["election"]
        if (
            overview.get("kind") != "ElectionOverview"
            or (election["name"], election["electionDate"], election["type"]) != ELECTION
            or election["id"] != manifest["electionId"]
            or election["resultsStatus"] not in ("Unofficial", "Official")
        ):
            raise ValueError("Nevada results belong to another election")
        counties = {c["code"]: " ".join(c["name"].split()) for c in overview["counties"]}
        parties = {p["id"]: p["abbreviation"] for p in overview["parties"]}
        if len(counties) != 17 or len(set(counties.values())) != 17:
            raise ValueError("Nevada results lack its 17 counties")
        wanted = state_adapter("NV").races
        selected = {}
        for contest in overview["contests"]:
            race = race_name(contest["name"])
            if race is None:
                continue
            if race not in wanted or race in selected or contest["voteFor"] != 1:
                raise ValueError(f"Unexpected or repeated Nevada contest {contest['name']!r}")
            sha = files.get(contest["id"])
            if sha is None:
                raise ValueError(f"{race}: the manifest names no Nevada results file")
            results = unique_json(member(bundle, f"{sha}.json", sha))
            if results.get("kind") != "ContestResults" or results["contestId"] != contest["id"]:
                raise ValueError(f"{race}: Nevada results file belongs to another contest")
            selected[race] = (contest, results)
    if set(selected) != set(wanted):
        raise ValueError("Nevada results lack a selected contest")
    rows, masked = [], {}
    for race, (contest, results) in selected.items():
        choices = {}
        for choice in contest["choices"]:
            if choice["kind"] == "NoneOfThese":
                choices[choice["id"]] = (*NONE_OF_THESE, "")
            elif choice["kind"] == "Candidate":
                party = parties.get(choice["candidate"]["partyId"])
                name = display(choice["label"])
                if not party or not name:
                    raise ValueError(f"{race}: Nevada candidate without a name or party")
                choices[choice["id"]] = (choice["id"], name, party)
            else:
                raise ValueError(f"{race}: unexpected Nevada choice kind {choice['kind']!r}")
        totals = contest["totals"]
        statewide = {t["choiceId"]: votes(t["votes"], "statewide votes") for t in totals["choices"]}
        if set(statewide) != set(choices) or sum(statewide.values()) != totals["totalVotes"]:
            raise ValueError(f"{race}: Nevada statewide totals do not add up")
        scope = contest["scope"]["countyCodes"]
        sums, received, hidden = dict.fromkeys(choices, 0), [], []
        for county in results["counties"]:
            code = county["countyCode"]
            if code not in scope or code in received or code in hidden:
                raise ValueError(f"{race}: unexpected or repeated Nevada county {code}")
            values = {c["choiceId"]: c["votes"] for c in county["choices"]}
            if set(values) != set(choices):
                raise ValueError(f"{race}: Nevada county {code} lists other choices")
            if county["lead"]["status"] == "Masked" and all(v is None for v in values.values()):
                hidden.append(code)
                continue
            received.append(code)
            for identity, value in values.items():
                key, name, party = choices[identity]
                sums[identity] += votes(value, "county votes")
                rows.append(row(race, contest["id"], key, name, party, value, counties[code]))
        if (
            sorted(received + hidden) != sorted(scope)
            or totals["receivedCountyCount"] != len(received)
            or totals["expectedCountyCount"] != len(scope)
        ):
            raise ValueError(f"{race}: Nevada counties received differ from the contest's")
        if sums != statewide:
            raise ValueError(f"{race}: Nevada counties do not add up to the statewide totals")
        for identity, value in statewide.items():
            key, name, party = choices[identity]
            rows.append(row(race, contest["id"], key, name, party, value))
        if hidden:
            masked[race] = sorted(counties[code] for code in hidden)
        names = [name for key, name, _ in choices.values() if key != NONE_OF_THESE[0]]
        verify_scope("NV", race, names, sorted(counties[code] for code in received))
    stamp = re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", manifest["generatedAtUtc"])
    generated = datetime.fromisoformat(stamp)
    if generated.tzinfo is None or generated < datetime.fromisoformat("2026-11-03T00:00:00+00:00"):
        raise ValueError("Nevada manifest has an unexpected generation time")
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=f"release {manifest['sequence']}, {manifest['generatedAtUtc']}",
        source_generated_at=generated.isoformat(),
        **parse_report.fields(election["resultsStatus"] == "Official"),
        masked_counties=masked,
        contest_total_reconciliation="passed",
    )
