"""Replay archived files into checked CSV snapshots; parsing never makes HTTP requests."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path

from election_pipeline.checks import validate_payload
from election_pipeline.parsers.nc import (
    DEMO_FIELDS,
    RESULT_FIELDS,
    VOTES,
    demographic_rows,
    integer,
    records,
    result_rows,
)
from election_pipeline.pipeline import collector_code_hash
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.storage import ArchiveStore, atomic_json, file_hash, utc_now

RESULT_COUNTY = (
    "election_date", "state", "county", "contest_id", "choice", "choice_party",
)

RESULT_CONTEST = (
    "election_date", "state", "contest_id", "choice", "choice_party",
)

DEMO_STATE = (
    "election_date",
    "state",
    "request_type",
    "party_registration",
    "sex",
    "race",
    "ethnicity",
)


def accumulate(groups, row, dimensions, measures):
    key = tuple(row[name] for name in dimensions)
    totals = groups.setdefault(key, [0] * len(measures))

    for index, measure in enumerate(measures):
        totals[index] += row[measure]


def write_groups(path, groups, dimensions, measures):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((*dimensions, *measures))

        for key, totals in sorted(groups.items()):
            writer.writerow((*key, *totals))


def reference_check(path, source, totals, limit):
    """Compare complete contest/choice totals; origin and timing need human verification."""
    expected = {}

    for key, values in totals.items():
        pair = (key[2], key[3])  # RESULT_CONTEST: contest_id, choice
        expected[pair] = expected.get(pair, 0) + values[-1]

    actual = {}
    columns = ("election_date", "contest_id", "choice", "total_votes")

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in records(handle, columns, ",", limit):
            if row["election_date"] != source.election_date:
                raise ValueError(f"Reference line {line}: wrong election date")

            key = (row["contest_id"], row["choice"])

            if key in actual:
                raise ValueError(f"Reference line {line}: duplicate contest/choice")

            actual[key] = integer(row["total_votes"], f"Reference line {line}")

    differences = [
        {
            "contest_id": key[0],
            "choice": key[1],
            "parsed": expected.get(key),
            "reference": actual.get(key),
        }
        for key in sorted(expected.keys() | actual.keys())
        if expected.get(key) != actual.get(key)
    ]

    return {
        "status": "mismatch" if differences else "matched",
        "differences": differences,
        "scope": "complete totals for all five tracked contests, including write-ins",
        "origin_and_publication_timing": "not verified automatically",
    }


def _parse(archive, source, settings, output, stats, reference):
    results = source.dataset == "election_results" and source.format == "zip"
    demographics = (
        source.dataset == "absentee_early_demographics" and source.format == "csv"
    )

    if source.state != "NC" or not (results or demographics):
        raise ValueError("No parser registered for this state/dataset/format")

    if reference is not None and not results:
        raise ValueError("Candidate-vote reference comparisons apply only to election results")

    validate_payload(archive, source, settings)

    fields = RESULT_FIELDS if results else DEMO_FIELDS
    measures = VOTES if results else ("ballot_count",)
    iterator = result_rows if results else demographic_rows
    county, statewide = {}, {}
    raw_totals = [0] * len(measures)

    name = "precinct_results.csv" if results else "county_demographics.csv"

    with (output / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()

        for row in iterator(archive, source, settings.max_csv_rows, stats):
            writer.writerow(row)

            for index, measure in enumerate(measures):
                raw_totals[index] += row[measure]

            if results:
                accumulate(county, row, RESULT_COUNTY, measures)
            else:
                accumulate(statewide, row, DEMO_STATE, measures)

    if results:
        for key, values in county.items():
            row = dict(zip((*RESULT_COUNTY, *measures), (*key, *values)))
            accumulate(statewide, row, RESULT_CONTEST, measures)

        write_groups(output / "county_results.csv", county, RESULT_COUNTY, measures)
        write_groups(output / "contest_results.csv", statewide, RESULT_CONTEST, measures)
    else:
        write_groups(output / "state_demographics.csv", statewide, DEMO_STATE, measures)

    # Internal transformation checks, not independent official validation.
    for groups in (county, statewide) if results else (statewide,):
        aggregated = [
            sum(values[index] for values in groups.values())
            for index in range(len(measures))
        ]

        if aggregated != raw_totals:
            raise ValueError("Aggregation did not preserve input counts")

    stats["internal_reconciliation"] = "passed"
    stats["all_counts_zero"] = not any(raw_totals)

    stats["reference_comparison"] = (
        reference_check(reference, source, statewide, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided" if results else "not_applicable"}
    )

    stats["warnings"] = []

    if stats["all_counts_zero"]:
        stats["warnings"].append(
            "All counts are zero; this does not establish live reporting."
        )

    if results:
        stats["warnings"].append(
            "Candidate votes are not unique ballots or demographic turnout."
        )

    stats["election_status"] = "not determined by parser"


def parse_source(
    source: Source,
    settings: CollectorSettings,
    data_dir: Path,
    *,
    sha256: str | None = None,
    reference: Path | None = None,
) -> dict:
    store = ArchiveStore(data_dir, source)
    report = {"source_id": source.source_id, "started_at": utc_now()}

    try:
        if sha256 is None:
            state = store.read_state()

            if state.get("url") != source.url or not state.get("sha256"):
                raise ValueError("No collected snapshot for this source URL; collect it first")

            sha256 = state["sha256"]

        archive = store.archive_path(sha256)

        if file_hash(archive) != sha256:
            raise ValueError("Raw archive SHA-256 mismatch")

        identity = {
            "raw_sha256": sha256,
            "source_signature": source.signature(settings),
            "code_sha256": collector_code_hash(),
            "reference_sha256": file_hash(reference) if reference is not None else None,
        }

        run = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        parent = store.root / "parsed" / source.source_id
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / f"{sha256[:16]}-{run[:16]}"

        if target.exists():
            previous = json.loads((target / "report.json").read_text(encoding="utf-8"))

            if previous["identity"] != identity:
                raise ValueError("Parsed output identity collision")

            for name, digest in previous["output_sha256"].items():
                if file_hash(target / name) != digest:
                    raise ValueError(f"Existing parsed output is corrupt: {name}")

            return {
                **previous,
                "outcome": "already_parsed",
                "output_dir": str(target),
            }

        report.update(
            identity=identity,
            election_date=source.election_date,
            dataset=source.dataset,
            source_url=source.url,
            raw_archive=store.relative(archive),
            parser_contract_version=1,
        )

        with tempfile.TemporaryDirectory(prefix=".parse-", dir=parent) as temporary:
            output = Path(temporary) / "snapshot"
            output.mkdir()
            stats = {}

            _parse(archive, source, settings, output, stats, reference)
            report.update(stats)

            report.update(
                outcome="parsed",
                finished_at=utc_now(),
                output_sha256={
                    path.name: file_hash(path)
                    for path in sorted(output.glob("*.csv"))
                },
            )

            if file_hash(archive) != sha256 or (
                reference is not None
                and file_hash(reference) != identity["reference_sha256"]
            ):
                raise ValueError("An input changed during parsing")

            atomic_json(output / "report.json", report)
            os.rename(output, target)

        return {**report, "output_dir": str(target)}

    except (OSError, ValueError, csv.Error, zipfile.BadZipFile) as error:
        report.update(
            outcome="validation_failed",
            error=str(error),
            finished_at=utc_now(),
        )

        path = (
            store.root
            / "reports"
            / "parsing"
            / source.source_id
            / f"{uuid.uuid4().hex}.json"
        )

        atomic_json(path, report)
        return {**report, "report_path": str(path)}