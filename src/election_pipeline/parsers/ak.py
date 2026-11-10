"""Alaska's dated first-choice CSV: validate every selected unit before summing."""

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from importlib.resources import files
from urllib.parse import urljoin

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.status import results_status
from election_pipeline.parsers.structured import row
from election_pipeline.states import state_adapter

CSV_URL = "https://www.elections.alaska.gov/enr/26GENR/results/GA_ENR_Precinct_State_of_Alaska.csv"
PAGE_URL = "https://www.elections.alaska.gov/election-results/e/?id=26genr"
SCOPE_SHA256 = "c5e4176549074331cb6e307550dfa4c95c7216ded34be0d125dbe3e0fb6ee873"
METHODS = ("Election Day", "Absentee", "Early Voting", "Question", "Remote")
MEASURES = ("ballots", "votes", "under_votes", "over_votes")
HEADER = (
    "Precinct_name",
    "Reporting_flag",
    "Pct_Id",
    "Pct_seq_nbr",
    "Reg_voters",
    "Aggregation_ID",
    "Contest_Id",
    "Contest_seq_nbr",
    "Contest_title",
    "Contest_party_name",
    "Selectable_Options",
    "Candidate_State_ID",
    "candidate_id",
    "candidate_name",
    "Candidate_Type",
    "cand_seq_nbr",
    "Party_Code",
    *("total_" + measure for measure in MEASURES),
    *(method + "_" + measure for method in METHODS for measure in MEASURES),
)


def validate_source(source):
    if (
        source.state != "AK"
        or source.election_date != "2026-11-03"
        or source.format != "bundle"
        or source.url != CSV_URL
        or source.primary_filename != "results.csv"
        or source.files != (("election.html", PAGE_URL),)
        or set(source.expected_members) != {"election.html", "results.csv"}
        or source.min_interval_seconds < 300
    ):
        raise ValueError("Alaska requires the reviewed election-dated official bundle")


def scope():
    raw = files("election_pipeline").joinpath("resources/ak_2026_scope.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCOPE_SHA256:
        raise ValueError("Alaska scope resource failed its integrity check")
    return json.loads(raw)


def normalized(values):
    # This export has one unnamed, empty trailing column. Other extra fields are errors.
    if len(values) == len(HEADER) + 1 and values[-1] == "":
        values = values[:-1]
    if len(values) != len(HEADER):
        raise ValueError("Unexpected Alaska CSV field count")
    return values


def validate_page(raw):
    """Check the election page, and return whether its results status reads official."""
    page = Tree(raw.decode("utf-8-sig"))
    headings = [n.text for n in page.root.find("h2")]
    if not any(
        re.fullmatch(r"2026 General Election\s*[–—-]\s*November 3, 2026", h) for h in headings
    ):
        raise ValueError("Alaska page belongs to another election or is a challenge page")
    if not any(urljoin(PAGE_URL, n.attrs.get("href", "")) == CSV_URL for n in page.root.find("a")):
        raise ValueError("Alaska election page no longer links the configured CSV")
    if not re.search(r"(?:first|1st)\s+choice.{0,60}Round\s*1", page.root.text, re.I):
        raise ValueError("Alaska report is not explicitly identified as first-choice Round 1")
    # The page also mentions "certified results" of past elections, outside this label.
    return results_status(page.root.text, "Results Status: {}", "Alaska")


def parse(archive_path, source, settings, output, stats, reference=None):
    validate_source(source)
    profile = scope()
    wanted = state_adapter("AK").races
    if set(wanted) != set(profile["races"]):
        raise ValueError("Alaska requires all three reviewed statewide contests")
    by_id = {value["source_id"]: key for key, value in profile["races"].items()}
    seen, identities, metadata = set(), set(), {}
    totals, method_totals, unit_votes, flags = (
        Counter(),
        defaultdict(Counter),
        defaultdict(Counter),
        Counter(),
    )
    input_rows = 0
    with zipfile.ZipFile(archive_path) as archive:
        official = validate_page(archive.read("election.html"))
        reader = csv.reader(
            io.StringIO(archive.read(source.primary_filename).decode("utf-8-sig")), strict=True
        )
        if tuple(normalized(next(reader, []))) != HEADER:
            raise ValueError("Unexpected Alaska CSV header")
        for line, values in enumerate(reader, 2):
            input_rows += 1
            if input_rows > settings.max_csv_rows:
                raise ValueError("Alaska row limit exceeded")
            raw = dict(zip(HEADER, (value.strip() for value in normalized(values))))
            race = by_id.get(raw["Contest_Id"])
            if race is None:
                if raw["Contest_title"] in {r["title"] for r in profile["races"].values()}:
                    raise ValueError("Alaska contest ID changed")
                continue
            expected = profile["races"][race]
            unit, candidate = raw["Pct_seq_nbr"], raw["candidate_id"]
            if (
                any("\0" in value for value in raw.values())
                or raw["Contest_title"] != expected["title"]
                or raw["Selectable_Options"] != "1"
                or raw["Candidate_Type"] != "C"
                or raw["Aggregation_ID"]
                or raw["Contest_party_name"]
                or raw["Reporting_flag"] not in {"0", "1"}
                or unit not in profile["units"]
                or unit in expected["excluded_units"]
                or candidate not in expected["candidates"]
                or [raw["candidate_name"], raw["Party_Code"]] != expected["candidates"][candidate]
                or [raw["Pct_Id"], " ".join(raw["Precinct_name"].split())] != profile["units"][unit]
            ):
                raise ValueError(f"Alaska line {line}: identity/scope differs from reviewed CSV")
            key = (race, unit, candidate)
            if key in seen:
                raise ValueError("Duplicate Alaska reporting-unit/candidate result")
            seen.add(key)
            identities.add((race, unit))
            flags[raw["Reporting_flag"]] += 1
            integer(raw["Reg_voters"], "registered voters")
            counts = {
                prefix + "_" + measure: integer(raw[prefix + "_" + measure], "Alaska count")
                for prefix in ("total", *METHODS)
                for measure in MEASURES
            }
            for measure in MEASURES:
                if counts["total_" + measure] != sum(counts[m + "_" + measure] for m in METHODS):
                    raise ValueError("Alaska vote-method totals do not reconcile")
            for prefix in ("total", *METHODS):
                if counts[prefix + "_votes"] > counts[prefix + "_ballots"]:
                    raise ValueError("Alaska candidate votes exceed ballots")
                method_totals[race][prefix] += counts[prefix + "_votes"]
                unit_votes[(race, unit)][prefix] += counts[prefix + "_votes"]
            unit_metadata = {
                prefix: tuple(
                    counts[prefix + "_" + measure]
                    for measure in ("ballots", "under_votes", "over_votes")
                )
                for prefix in ("total", *METHODS)
            }
            unit_key = (race, unit)
            if unit_key in metadata and metadata[unit_key] != unit_metadata:
                raise ValueError("Alaska ballot metadata differs between candidates in one unit")
            metadata[unit_key] = unit_metadata
            totals[(race, candidate)] += counts["total_votes"]

    required = {
        (race, unit, candidate)
        for race, definition in profile["races"].items()
        for unit in profile["units"]
        if unit not in definition["excluded_units"]
        for candidate in definition["candidates"]
    }
    if seen != required:
        raise ValueError("Alaska candidate/reporting-unit coverage is incomplete")
    # The CSV omits a write-in category. Record positive residuals without inventing
    # candidate rows or assigning these ballots to a party. Reject negative residuals.
    residuals, ballots = Counter(), Counter()
    for race, unit in identities:
        values = metadata[(race, unit)]
        for prefix, (cast, under, over) in values.items():
            residual = cast - under - over - unit_votes[(race, unit)][prefix]
            if residual < 0:
                raise ValueError("Alaska candidate votes and under/overvotes exceed unit ballots")
            if prefix == "total":
                residuals[race] += residual
        # Repeated ballot metadata is counted once per reporting unit, not per candidate.
        ballots[race] += values["total"][0]
    rows = [
        row(
            race,
            profile["races"][race]["source_id"],
            candidate,
            *profile["races"][race]["candidates"][candidate],
            votes,
        )
        for (race, candidate), votes in sorted(totals.items())
    ]
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        input_rows=input_rows,
        selected_rows=len(seen),
        vote_basis="first_choice_listed_candidates",
        vote_round=1,
        official_results=official,
        reporting_unit_counts={r: sum(race == r for race, _ in identities) for r in wanted},
        candidate_roster="matched_reviewed_capture",
        reporting_unit_roster="matched_reviewed_capture",
        vote_method_reconciliation="passed",
        reporting_flags=dict(flags),
        ballot_totals=dict(ballots),
        ballot_residuals=dict(residuals),
        vote_method_totals={r: dict(values) for r, values in method_totals.items()},
    )
    stats["warnings"] += [
        "First-choice Round 1 only; later ranked-choice tabulations are not included.",
        "CSV-listed candidates only. Ballot residuals are not assigned to write-ins or parties.",
        "Shares use listed candidate votes. Reporting_flag is not a percent counted.",
    ]
    stats["reference_comparison"]["scope"] = (
        "All CSV-listed candidates in the three selected contests"
    )
