"""Strict readers shared by the new official feed adapters."""

import json
import re
import xml.etree.ElementTree as ET


def text(value, label="text"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {label}")
    return " ".join(value.split())


def count(value):
    if type(value) is not int or not 0 <= value <= 9223372036854775807:
        raise ValueError("Expected a nonnegative bigint vote count")
    return value


def formatted_count(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)", value
    ):
        raise ValueError(f"Invalid formatted vote count: {value!r}")
    return count(int(value.replace(",", "")))


def unique_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


def xml(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML entities and doctypes are unsupported")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError("Malformed XML document") from error
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    return root


def row(
    contest, source_contest, candidate, name, party, votes, county="", reporting=None, total=None
):
    return dict(
        unit_type="county" if county else "state",
        county=county,
        contest_id=contest,
        source_contest_id=text(source_contest, "contest ID"),
        source_candidate_id=text(candidate, "candidate ID"),
        choice=text(name, "candidate name"),
        choice_party=party or "",
        total_votes=count(votes),
        precincts_reporting=reporting,
        precincts_total=total,
    )


def party(value):
    return {"R": "REP", "D": "DEM", "L": "LIB", "G": "GRN"}.get(value.upper(), value.upper())
