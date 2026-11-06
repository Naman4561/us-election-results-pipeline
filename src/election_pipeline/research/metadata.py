"""Metadata shared by file imports and the research catalog."""

from __future__ import annotations

import re
from datetime import date, datetime

from election_pipeline.settings import validate_url

KINDS = (
    "election_results",
    "demographics",
    "census",
    "participation",
    "voter_registration",
    "other",
)
STATUSES = ("unofficial", "certified", "published", "provisional", "unknown", "synthetic")
GEOGRAPHIES = (
    "national",
    "state",
    "county",
    "precinct",
    "district",
    "tract",
    "block_group",
    "mixed",
    "other",
)
COLUMN_TYPES = ("text", "integer", "number")
REQUIRED = (
    "dataset_id",
    "title",
    "kind",
    "status",
    "jurisdiction",
    "geography",
    "grain",
    "period_start",
    "publisher",
    "source_url",
)
OPTIONAL = (
    "period_end",
    "election_date",
    "source_as_of",
    "tags",
    "notes",
    "column_types",
    "parent_sha256",
)


def clean_metadata(value: dict) -> dict:
    if not isinstance(value, dict) or value.keys() - set(REQUIRED + OPTIONAL):
        raise ValueError("The manifest must be an object with supported metadata fields")
    result = {}
    for name in REQUIRED:
        field = value.get(name)
        if not isinstance(field, str) or not field.strip() or len(field) > 2000:
            raise ValueError(f"Manifest requires a nonempty {name}")
        result[name] = field.strip()

    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,95}", result["dataset_id"]):
        raise ValueError("dataset_id must be a lowercase identifier of up to 96 characters")
    for name, choices in (("kind", KINDS), ("status", STATUSES), ("geography", GEOGRAPHIES)):
        if result[name] not in choices:
            raise ValueError(f"Invalid {name}. Choose from {', '.join(choices)}")
    result["jurisdiction"] = result["jurisdiction"].upper()
    validate_url(result["source_url"])

    for name in ("period_start", "period_end", "election_date"):
        field = value.get(name, result.get(name))
        if field is not None:
            if not isinstance(field, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", field):
                raise ValueError(f"{name} must use YYYY-MM-DD")
            result[name] = date.fromisoformat(field).isoformat()
    if result.get("period_end", result["period_start"]) < result["period_start"]:
        raise ValueError("period_end precedes period_start")

    if (as_of := value.get("source_as_of")) is not None:
        if not isinstance(as_of, str):
            raise ValueError("source_as_of must be an ISO timestamp with a UTC offset")
        parsed = datetime.fromisoformat(as_of)
        if parsed.utcoffset() is None:
            raise ValueError("source_as_of must include a UTC offset")
        result["source_as_of"] = parsed.isoformat()

    tags = value.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > 32
        or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 80 for tag in tags)
    ):
        raise ValueError("Use up to 32 nonempty text tags of at most 80 characters")
    result["tags"] = sorted({tag.strip().lower() for tag in tags})

    column_types = value.get("column_types", {})
    if not isinstance(column_types, dict) or any(
        not isinstance(name, str) or kind not in COLUMN_TYPES for name, kind in column_types.items()
    ):
        raise ValueError("column_types maps column names to text, integer, or number")
    if column_types:
        result["column_types"] = column_types
    if (notes := value.get("notes")) is not None:
        if not isinstance(notes, str) or len(notes) > 5000:
            raise ValueError("notes must be text of at most 5,000 characters")
        result["notes"] = notes.strip()
    if (parent := value.get("parent_sha256")) is not None:
        if not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{64}", parent):
            raise ValueError("parent_sha256 must be a lowercase SHA-256")
        result["parent_sha256"] = parent
    return result
