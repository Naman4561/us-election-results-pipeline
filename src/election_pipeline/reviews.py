"""Signed reviews: an operator's transcription of an official report no reader takes.

A county or authority report that is a scan, or in a one-off layout, enters a manual
import as the report itself plus a review: the counts the operator transcribed from it,
bound to the report's exact bytes and signed by whoever checked them against it. One
format, one validator (``read``) and one tool (``scripts/review.py``) serve every state
that takes reviews (Illinois by authority, Wisconsin by county).

    {"schema": "election-review/1",
     "state": "WI", "place": "Vilas",              # the county or election authority
     "report": "canvassed" | "unofficial",
     "file_sha256": [the report's SHA-256, or each part's, in order],
     "transcribed_by": "...",
     "races": {race: {line: votes}},
     "totals": {race: printed total},            # where the report prints one
     "precincts": {race: [reporting, total]},    # where it prints them
     "signed_by": "...", "signed_on": "YYYY-MM-DD"}

Reviews written before this format (Illinois's ``authority``, Wisconsin's ``county``,
both ``reviewed_by`` and ``reviewed_on``) stay readable unchanged, so every archived
import parses as it did. Mississippi's county reviews and the certified-results CSV
carry their own contracts and keep their readers (parsers/county_reports.py,
parsers/certified.py).
"""

from __future__ import annotations

import re

from election_pipeline.parsers.structured import count

SCHEMA = "election-review/1"
REPORTS = ("canvassed", "unofficial")


def place(document):
    """The county or authority a review is for (``place``; earlier ``authority`` or
    ``county``)."""
    for key in ("place", "authority", "county"):
        if isinstance(document.get(key), str):
            return document[key]
    return None


def signer(document):
    """Who signed it (``signed_by``; earlier ``reviewed_by``), or ""."""
    return str(document.get("signed_by") or document.get("reviewed_by") or "").strip()


def files(document):
    listed = document.get("file_sha256")
    return [listed] if isinstance(listed, str) else listed


def read(document, sha256s, label):
    """{race: (lines, precincts or None)} from a signed review of the report whose files
    have ``sha256s``; refused when unsigned, for other files, or not adding up to the
    totals the report prints."""
    if document.get("schema", SCHEMA) != SCHEMA:
        raise ValueError(f"{label}: review is in an unknown format")
    if files(document) != sha256s or not signer(document):
        raise ValueError(f"{label}: review is unsigned or for another file")
    found = {}
    for race, counts in document["races"].items():
        lines = [(name, count(votes)) for name, votes in counts.items()]
        printed = (document.get("totals") or {}).get(race)
        if printed is not None and sum(v for _, v in lines) != printed:
            raise ValueError(f"{label} {race}: reviewed counts do not add up to the report")
        precincts = (document.get("precincts") or {}).get(race)
        if precincts is not None:
            reporting, total = (count(n) for n in precincts)
            if reporting > total or total == 0:
                raise ValueError(f"{label} {race}: unexpected precincts reporting")
            precincts = (reporting, total)
        found[race] = (lines, precincts)
    return found


def transcription(spec, sha256s):
    """An unsigned review of a report whose files have ``sha256s``, from an operator's
    transcription (``state``, ``place``, ``report``, ``transcribed_by``, ``races``, and
    optionally ``totals``, ``precincts`` and ``percentages`` {race: {line: percent}});
    refused where the counts disagree with what the report prints."""
    missing = {"state", "place", "report", "transcribed_by", "races"} - set(spec)
    if missing:
        raise ValueError(f"The transcription lacks {sorted(missing)}")
    if spec["report"] not in REPORTS or not re.fullmatch(r"[A-Z]{2}", spec["state"]):
        raise ValueError("The transcription's state or report kind is invalid")
    for race, lines in spec["races"].items():
        total = sum(count(v) for v in lines.values())
        printed = (spec.get("totals") or {}).get(race)
        if printed is not None and printed != total:
            raise ValueError(f"{race}: lines add up to {total}, the report prints {printed}")
        for line, percent in (spec.get("percentages") or {}).get(race, {}).items():
            if abs(round(100 * lines[line] / total, 2) - percent) > 0.011:
                raise ValueError(f"{race} {line}: {lines[line]} of {total} is not {percent}%")
    return {
        "schema": SCHEMA,
        **{k: spec[k] for k in ("state", "place", "report", "transcribed_by", "races")},
        "file_sha256": list(sha256s),
        "totals": spec.get("totals") or {},
        "precincts": spec.get("precincts") or {},
        "signed_by": "",
        "signed_on": "",
    }


def signed(document, signer_name, day):
    """The review signed by ``signer_name`` on ``day`` (YYYY-MM-DD); once only."""
    if signer(document):
        raise ValueError(f"{place(document)} is already signed by {signer(document)}")
    if not signer_name.strip() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Sign with a name and a date (YYYY-MM-DD)")
    return {**document, "signed_by": signer_name.strip(), "signed_on": day}
