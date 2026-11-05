"""County-specific format parsing and explicit review of otherwise unsupported originals."""

import hashlib
import re
from datetime import date, datetime

from election_pipeline.settings import validate_url


def reviewed_file(raw, county, contract, review):
    from election_pipeline.parsers.ms import VisibleText, local_stamp, pdf_text, stamp

    if not isinstance(review, dict) or (
        review.get("state") != contract["state"]
        or review.get("county") != county["key"]
        or review.get("election_date") != contract["election_date"]
        or review.get("contest_id") != contract["contest_id"]
        or review.get("raw_sha256") != hashlib.sha256(raw).hexdigest()
        or review.get("countywide_report_confirmed") is not True
        or not isinstance(review.get("reviewed_by"), str)
        or not review["reviewed_by"].strip()
        or len(raw) > 8_000_000
        or not raw.startswith((b"%PDF-", b"<!DOCTYPE", b"<!doctype", b"<html"))
    ):
        raise ValueError("Review must identify the original countywide official report")
    text = pdf_text(raw) if raw.startswith(b"%PDF-") else VisibleText(raw.decode("utf-8-sig")).text
    if len(text.strip()) > 80 and (
        not re.search(r"\b" + re.escape(county["name"]) + r"\b", text, re.I)
        or not re.search(r"(?:November|Nov\.?)\s+0?3,?\s+2026|11/0?3/2026", text, re.I)
        or not all(n in text for n in contract["candidates"])
    ):
        raise ValueError("Readable original disagrees with reviewed county/election identity")
    for key in ("source_url", "official_page_url"):
        validate_url(review.get(key, ""))
        if not review[key].startswith("https://"):
            raise ValueError("Official source links require HTTPS")
    counts = review.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != set(contract["candidates"])
        or any(type(n) is not int or not 0 <= n <= 10_000_000 for n in counts.values())
    ):
        raise ValueError("Invalid reviewed candidate counts")
    write_in = review.get("write_in_votes")
    printed = review.get("reported_candidate_vote_total")
    if write_in is not None and (
        type(write_in) is not int or write_in < 0 or not review.get("write_in_label")
    ):
        raise ValueError("Preserve write-in count and label")
    if printed is not None and (
        type(printed) is not int or write_in is None or printed != sum(counts.values()) + write_in
    ):
        raise ValueError("Reviewed candidate total does not reconcile")
    reporting, total = review.get("precincts_reporting"), review.get("precincts_total")
    if (reporting is None) != (total is None) or (
        total is not None
        and (
            type(reporting) is not int
            or type(total) is not int
            or not 0 <= reporting <= total <= 1000
        )
    ):
        raise ValueError("Invalid reviewed precinct coverage")
    for key in ("absentee_scope", "affidavit_scope"):
        if review.get(key) not in {"included", "excluded", "unspecified"}:
            raise ValueError("Explicit ballot scope is required")
    observed = stamp(review.get("observed_at"))
    generated = review.get("report_generated_at")
    if generated is not None:
        generated = stamp(generated)
        local_stamp(generated.replace(tzinfo=None).isoformat())
        if generated > observed:
            raise ValueError("Report generation follows observation")
    reported_date = review.get("report_date")
    if reported_date is not None and not date(2026, 11, 3) <= date.fromisoformat(
        reported_date
    ) < date(2027, 1, 1):
        raise ValueError("Unsupported report date")
    if generated and reported_date and generated.date().isoformat() != reported_date:
        raise ValueError("Report dates disagree")
    if generated is None and reported_date is None and not review.get("report_time_note"):
        raise ValueError("Explain why report date/time is unknown")
    return dict(
        counts=counts,
        write_in_votes=write_in,
        write_in_label=review.get("write_in_label"),
        report_generated_at=generated.isoformat() if generated else None,
        report_date=generated.date().isoformat() if generated else reported_date,
        precincts_reporting=reporting,
        precincts_total=total,
    )


def sunflower_hart(raw, county, contract):
    from election_pipeline.parsers.ms import (
        NUMBER,
        VisibleText,
        identity,
        integer,
        local_stamp,
        one,
    )

    text = VisibleText(raw.decode("utf-8-sig")).text
    identity(text, county)
    section = one(
        r"United States Senate - Vote for ONE (.*?) US House Of Rep", text, "Senate table"
    )
    body = section.split("Total ", 1)[-1].split("Cast Votes:", 1)[0].strip()
    remainder = body
    counts = {}
    for name, party in contract["candidates"].items():
        pattern = re.escape(name) + r" (Dem|Rep|Ind)" + (r" (" + NUMBER + r") [0-9.]+%") * 5
        values = one(pattern, body, name)
        remainder = re.sub(pattern, "", remainder)
        methods = [integer(n) for n in values[1:]]
        if {"Dem": "DEM", "Rep": "REP", "Ind": "IND"}[values[0]] != party or sum(
            methods[:-1]
        ) != methods[-1]:
            raise ValueError("County party/method counts disagree")
        counts[name] = methods[-1]
    if remainder.strip():
        raise ValueError("Unexpected Senate candidate")
    total = one(
        r"Cast Votes: (?:" + NUMBER + r" [0-9.]+% ){4}(" + NUMBER + r") [0-9.]+%",
        section,
        "cast vote total",
    )
    if integer(total) != sum(counts.values()):
        raise ValueError("Senate total does not reconcile")
    reporting, total = one(r"Precincts Reporting ([0-9]+) of ([0-9]+)", text, "reporting precincts")
    clock, day = one(
        r"Run Time ([0-9:]+ (?:AM|PM)) Run Date (11/[0-9]{2}/2026)", text, "report time"
    )
    generated = local_stamp(datetime.strptime(day + " " + clock, "%m/%d/%Y %I:%M %p").isoformat())
    return dict(
        counts=counts,
        write_in_votes=None,
        write_in_label=None,
        report_generated_at=generated,
        report_date=generated[:10],
        precincts_reporting=int(reporting),
        precincts_total=int(total),
    )
