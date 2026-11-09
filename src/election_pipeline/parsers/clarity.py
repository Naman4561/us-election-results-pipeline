"""Shared readers for official Clarity (Scytl) election-night files (Colorado, Iowa,
West Virginia, South Carolina).

Every bundle holds one published version's ``summary.json`` (statewide totals, the
figures the official summary page shows), ``settings.json`` (the site's own
``electionsettings.json``: state and last update time) and ``detail.zip`` (the official
"Download reports" county detail). State modules parse the detail format they publish
and check it against the summary.
"""

import gzip
import io
import re
import zipfile
from datetime import datetime

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, text, unique_json, xml


def document(raw, settings):
    """JSON bytes as served. Clarity's CDN may gzip a file even when asked not to; such a
    body is decompressed here, within the configured size budget."""
    if raw[:2] != b"\x1f\x8b":
        return raw
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        body = stream.read(settings.max_uncompressed_bytes + 1)
    if len(body) > settings.max_uncompressed_bytes:
        raise ValueError("Clarity document exceeds its uncompressed budget")
    return body


def settings_check(raw, state, settings):
    """The site's own state code and update time, which must fall in the election window."""
    record = unique_json(document(raw, settings))
    if record.get("entityname") != state or record.get("parententityname") is not None:
        raise ValueError(f"Clarity settings belong to another site than {state}")
    updated = text(record.get("websiteupdatedat"), "update time")
    match = re.fullmatch(r"(.+) [ECM][SD]T", updated)
    if not match:
        raise ValueError("Unexpected Clarity update time")
    when = datetime.strptime(match[1], "%m/%d/%Y %I:%M:%S %p")
    if not datetime(2026, 11, 3) <= when <= datetime(2026, 12, 31):
        raise ValueError("Clarity update time is outside this election's window")
    return updated


def summary(raw, races, settings):
    """Statewide summary rows for the selected races: {race: (key, [(name, party, votes)])}."""
    found = {}
    rows = unique_json(document(raw, settings))
    if not isinstance(rows, list):
        raise ValueError("Clarity summary is not a contest list")
    for contest in rows:
        race = races(text(contest["C"], "contest name"))
        if race is None:
            continue
        if race in found:
            raise ValueError(f"{race}: duplicate Clarity contest")
        names, parties, votes = contest["CH"], contest["P"], contest["V"]
        if (
            contest["VF"] != 1
            or not isinstance(names, list)
            or not names
            or len(names) != len(parties)
            or len(names) != len(votes)
            or len(set(names)) != len(names)
        ):
            raise ValueError(f"{race}: malformed or multi-seat Clarity contest")
        choices = [
            (text(n, "candidate"), (p or "").strip(), count(v))
            for n, p, v in zip(names, parties, votes)
        ]
        if contest["T"] != [sum(v for _, _, v in choices)]:
            raise ValueError(f"{race}: Clarity candidate counts disagree with the contest total")
        found[race] = (text(str(contest["K"]), "contest key"), choices)
    return found


def detail_member(raw, settings, suffix):
    """The single report file inside the official detail download."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (
            len(entries) != 1
            or not re.fullmatch(r"detail\." + suffix, entries[0].filename)
            or entries[0].flag_bits & 1
            or entries[0].file_size > settings.max_uncompressed_bytes
        ):
            raise ValueError("Unexpected Clarity detail download")
        return archive.read(entries[0])


def number(value, label, state):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,11}", value):
        raise ValueError(f"Invalid {state} {label}")
    return int(value)


def choice_key(value, state):
    """A choice key as a number; some states zero-pad it ("001")."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}", value):
        raise ValueError(f"Invalid {state} choice key")
    return int(value)


def detail_contests(root, wanted, race, code, state, election="2026 General"):
    if (
        text(root.findtext("ElectionName"), "election name") != election
        or text(root.findtext("ElectionDate"), "election date") != "11/3/2026"
        or text(root.findtext("Region"), "region") != code
    ):
        raise ValueError(f"{state} detail report belongs to another election")
    found = {}
    for contest in root.findall("Contest"):
        identity = race(text(contest.get("text"), "contest name"))
        if identity not in wanted:
            continue
        if (
            identity in found
            or contest.get("voteFor") != "1"
            or contest.get("isQuestion") != "false"
        ):
            raise ValueError(f"{identity}: unexpected or duplicate {state} contest")
        found[identity] = contest
    if set(found) != set(wanted):
        raise ValueError(f"{state} detail report lacks a selected contest")
    return found


def parse_detail_xml(
    archive_path, source, settings, output, stats, reference, *, state, race, vote_type, same_name
):
    """Totals by county from the "Detail XML" report of a state that publishes one vote
    type. Each county's candidate counts must add up to the statewide count, which must
    equal the official summary."""
    from election_pipeline.states import state_adapter

    code = source.state
    wanted = state_adapter(code).races
    with zipfile.ZipFile(archive_path) as archive:
        updated = settings_check(archive.read("settings.json"), code, settings)
        official = summary(archive.read("summary.json"), race, settings)
        root = xml(detail_member(archive.read("detail.zip"), settings, "xml"))
    found = detail_contests(root, wanted, race, code, state)
    if set(official) != set(wanted):
        raise ValueError(f"{state} summary lacks a selected contest")
    rows = []
    for identity, contest in found.items():
        key, expected = official[identity]
        if contest.get("key") != key:
            raise ValueError(f"{identity}: detail and summary contest keys differ")
        precincts = {}
        for county in contest.findall("ParticipatingCounties/County"):
            name = text(county.get("name"), "county")
            done = number(county.get("precinctsReported"), "precinct count", state)
            total = number(county.get("precinctsParticipating"), "precinct count", state)
            if name in precincts or done > total:
                raise ValueError(f"{identity}: invalid county reporting status")
            precincts[name] = (done, total)
        choices = contest.findall("Choice")
        if len(choices) != len(expected):
            raise ValueError(f"{identity}: detail and summary candidates differ")
        candidates = []
        for choice, (listed, party, votes) in zip(choices, expected, strict=True):
            identity_key = f"{key}-{choice_key(choice.get('key'), state)}"
            name = text(choice.get("text"), "candidate")
            if (
                not same_name(name, listed)
                or (choice.get("party") or "").strip() != party
                or number(choice.get("totalVotes"), "vote count", state) != votes
            ):
                raise ValueError(f"{identity}: detail and summary candidate totals differ")
            types = choice.findall("VoteType")
            if [t.get("name") for t in types] != [vote_type]:
                raise ValueError(f"{identity}: unexpected {state} vote types")
            if number(types[0].get("votes"), "vote count", state) != votes:
                raise ValueError(f"{identity}: vote type total differs from candidate total")
            candidates.append(identity_key)
            rows.append(row(identity, key, identity_key, name, party, votes))
            seen = set()
            for county in types[0].findall("County"):
                place = text(county.get("name"), "county")
                if place in seen or place not in precincts:
                    raise ValueError(f"{identity}: unexpected or duplicate county {place}")
                seen.add(place)
                done, total = precincts[place]
                rows.append(
                    row(
                        identity,
                        key,
                        identity_key,
                        name,
                        party,
                        number(county.get("votes"), "vote count", state),
                        county=place,
                        reporting=done,
                        total=total,
                    )
                )
            if seen != set(precincts):
                raise ValueError(f"{identity}: candidate lacks a participating county")
        verify_scope(code, identity, candidates, sorted(precincts))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        source_timestamp=text(root.findtext("Timestamp"), "timestamp"),
        contest_total_reconciliation="passed",
    )
