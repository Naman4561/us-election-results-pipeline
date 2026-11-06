"""Shared readers for official Clarity (Scytl) election-night files (Colorado, Iowa).

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

from election_pipeline.parsers.structured import count, text, unique_json


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
    match = re.fullmatch(r"(.+) [CM][SD]T", updated)
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
