"""Compare frozen results with a separately captured official reference."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from election_pipeline.database import connect
from election_pipeline.parsers.nc import COVERAGE
from election_pipeline.parsing import reference_check
from election_pipeline.storage import atomic_json, file_hash, utc_now


def compare_publication(
    db,
    publication_id,
    reference,
    *,
    evidence_dir,
    reference_url,
    observed_at,
    same_reporting_cutoff=False,
):
    """Require agreement for every choice in all five covered races."""
    url = urlsplit(reference_url)

    if (
        url.scheme != "https"
        or not url.netloc
        or url.username
        or url.password
    ):
        raise ValueError("Supply the HTTPS URL of the official reference")

    timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))

    if timestamp.utcoffset() is None:
        raise ValueError(
            "Reference observed_at must include its time-zone offset"
        )

    publication_id = uuid.UUID(str(publication_id))
    directory = Path(evidence_dir) / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)

    captured = directory / "reference.csv"
    shutil.copyfile(reference, captured)

    with connect(db) as conn, conn.transaction():
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )

        metadata = conn.execute(
            """
            SELECT *
            FROM serving.publications
            WHERE publication_id = %s
            """,
            (publication_id,),
        ).fetchone()

        if (
            not metadata
            or metadata["dataset"] != "election_results"
            or metadata["state"] != "NC"
        ):
            raise ValueError("Choose an NC results publication")

        rows = conn.execute(
            """
            SELECT contest_id, choice, choice_party, total_votes
            FROM serving.nc_contest_candidate_results
            WHERE publication_id = %s
            """,
            (publication_id,),
        ).fetchall()

    if {row["contest_id"] for row in rows} != set(COVERAGE.values()):
        raise ValueError(
            "Publication does not contain the complete five-race watchlist"
        )

    date = str(metadata["election_date"])
    totals = {
        (
            date,
            "NC",
            row["contest_id"],
            row["choice"],
            row["choice_party"],
        ): [row["total_votes"]]
        for row in rows
    }

    result = reference_check(
        captured,
        SimpleNamespace(election_date=date),
        totals,
        100_000,
    )

    all_zero = not any(row["total_votes"] for row in rows)
    outcome = "mismatch"

    if result["status"] == "matched":
        if all_zero:
            outcome = "pre_reporting_only"
        elif same_reporting_cutoff:
            outcome = "matched"
        else:
            outcome = "counts_match_cutoff_unverified"

    report = {
        "schema_version": 1,
        "outcome": outcome,
        "checked_at": utc_now(),
        "publication_id": str(publication_id),
        "release_id": metadata["release_id"],
        "source_id": metadata["source_id"],
        "raw_sha256": metadata["raw_sha256"],
        "reference_sha256": file_hash(captured),
        "reference_url": reference_url,
        "reference_observed_at": observed_at,
        "same_reporting_cutoff_attested": same_reporting_cutoff,
        "all_counts_zero": all_zero,
        "comparison": result,
        "official_origin": (
            "supplied by operator; URL ownership not verified by this command"
        ),
        "report_path": str(directory / "report.json"),
    }

    atomic_json(directory / "report.json", report)
    return report