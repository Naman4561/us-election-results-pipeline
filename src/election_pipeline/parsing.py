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
from election_pipeline.parsers.common import integer, records
from election_pipeline.pipeline import collector_code_hash
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.storage import ArchiveStore, atomic_json, file_hash, utc_now


def reference_check(path, source, totals, limit):
    """Compare complete contest/choice totals; origin and timing need human verification."""
    expected = {}

    for key, values in totals.items():
        pair = (key[2], key[3])  # contest_results.csv key: ..., contest_id, choice
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
        "scope": "complete totals for all configured contests, including write-ins",
        "origin_and_publication_timing": "not verified automatically",
    }


HANDLED = (OSError, ValueError, KeyError, TypeError, csv.Error, zipfile.BadZipFile)


def _parse(archive, source, settings, output, stats, reference):
    from election_pipeline.states import parser_for

    validate_payload(archive, source, settings)
    parser = parser_for(source)
    try:
        return parser(archive, source, settings, output, stats, reference)
    except HANDLED:
        raise
    except Exception as error:
        # An unexpected document shape (a missing line, element or attribute) is a failed
        # validation of that snapshot, reported like any other, not a crashed run.
        raise ValueError(f"Unexpected {type(error).__name__} while parsing: {error}") from error


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
            parser_contract_version=2,
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
                output_sha256={path.name: file_hash(path) for path in sorted(output.glob("*.csv"))},
            )

            if file_hash(archive) != sha256 or (
                reference is not None and file_hash(reference) != identity["reference_sha256"]
            ):
                raise ValueError("An input changed during parsing")

            atomic_json(output / "report.json", report)
            os.rename(output, target)

        return {**report, "output_dir": str(target)}

    except HANDLED as error:
        report.update(
            outcome="validation_failed",
            error=str(error),
            finished_at=utc_now(),
        )

        path = store.root / "reports" / "parsing" / source.source_id / f"{uuid.uuid4().hex}.json"

        atomic_json(path, report)
        return {**report, "report_path": str(path)}
