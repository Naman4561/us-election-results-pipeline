"""Validate isolated candidate tables and atomically publish frozen outputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from importlib.metadata import version
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from election_pipeline.database import connect

VOTES = "election_day early_voting absentee_by_mail provisional total_votes"
GROUPS = "request_type party_registration sex race ethnicity ballot_count"

# dataset -> (candidate layer, candidate model, serving table, copied columns)
TARGETS = {
    "election_results": [
        (
            "marts",
            "nc_county_candidate_results",
            "nc_county_candidate_results",
            f"county contest_id choice choice_party {VOTES}",
        ),
        (
            "marts",
            "nc_contest_candidate_results",
            "nc_contest_candidate_results",
            f"contest_id choice choice_party {VOTES} contest_votes vote_share",
        ),
    ],
    "absentee_early_demographics": [
        (
            "staging",
            "stg_nc_absentee_demographics",
            "nc_county_absentee_demographics",
            f"county {GROUPS}",
        ),
        (
            "marts",
            "nc_state_absentee_demographics",
            "nc_state_absentee_demographics",
            GROUPS,
        ),
    ],
}


def project_hash(root):
    paths = [
        root / "pyproject.toml",
        root / "scripts/run_dbt.py",
        root / "scripts/publish.py",
        Path(__file__).resolve(),
    ]
    paths += list((root / "dbt").glob("*.yml"))

    for directory in ("models", "macros", "tests"):
        paths += [
            p
            for p in (root / "dbt" / directory).rglob("*")
            if p.suffix in (".sql", ".yml", ".yaml")
        ]

    digest = hashlib.sha256()

    for package in (
        "dbt-core", "dbt-postgres", "dbt-adapters", "dbt-common", "psycopg"
    ):
        digest.update(f"{package}={version(package)}".encode() + b"\0")

    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")

    return digest.hexdigest()


def release_metadata(conn, release_id):
    row = conn.execute(
        """
        SELECT r.release_id, r.snapshot_id, r.loaded_at, r.expected_rows,
               r.loaded_rows, s.source_id, s.state, s.election_date,
               s.dataset, s.source_url, f.raw_sha256,
               (SELECT min(a.finished_at) FROM ops.fetch_attempts a
                WHERE a.snapshot_id = r.snapshot_id
                  AND a.outcome = 'downloaded') AS first_observed_at,
               (SELECT max(a.source_modified_at) FROM ops.fetch_attempts a
                WHERE a.snapshot_id = r.snapshot_id) AS source_modified_at
        FROM ops.processing_releases r
        JOIN ops.snapshots f USING (snapshot_id)
        JOIN ops.sources s USING (source_id)
        WHERE r.release_id = %s
        """,
        (release_id,),
    ).fetchone()

    if (
        not row
        or row["loaded_at"] is None
        or row["loaded_rows"] != row["expected_rows"]
    ):
        raise ValueError("Choose a completed ingestion release")

    if row["state"] != "NC" or row["dataset"] not in TARGETS:
        raise ValueError("Publication currently supports the two NC datasets only")

    return row


def checked_results(path):
    manifest = json.loads(
        (path / "manifest.json").read_text(encoding="utf-8")
    )
    results = json.loads(
        (path / "run_results.json").read_text(encoding="utf-8")
    )

    invocation = results["metadata"]["invocation_id"]
    if not invocation or invocation != manifest["metadata"]["invocation_id"]:
        raise ValueError("dbt artifacts do not belong to one invocation")

    if any(
        node["resource_type"] == "model"
        and node["config"]["materialized"] != "table"
        for node in manifest["nodes"].values()
    ):
        raise ValueError("Publication candidates must be materialized as tables")

    if not manifest.get("unit_tests") or not any(
        node["resource_type"] == "test"
        and node.get("name") == "release_completeness"
        for node in manifest["nodes"].values()
    ):
        raise ValueError("Required publication checks are missing from the project")

    expected = {
        key
        for key, node in manifest["nodes"].items()
        if node["resource_type"] in ("model", "test")
    }
    expected.update(manifest.get("unit_tests", {}))
    actual = {r["unique_id"]: r for r in results["results"]}

    if (
        not expected
        or set(actual) != expected
        or len(actual) != len(results["results"])
    ):
        raise ValueError("The complete dbt build did not run")

    if any(r["status"] not in ("success", "pass") for r in actual.values()):
        raise ValueError("A dbt node failed, warned, or was skipped")

    return results


def build_candidate(root, db, metadata, schema, artifacts):
    source_variable = (
        "nc_results_source"
        if metadata["dataset"] == "election_results"
        else "nc_demographics_source"
    )
    variables = {
        "publication_materialization": "table",
        "publication_release_id": metadata["release_id"],
        "election_date": str(metadata["election_date"]),
        source_variable: metadata["source_id"],
    }

    environment = {
        **os.environ,
        **{
            "POSTGRES_" + key: str(db[value])
            for key, value in (
                ("HOST", "host"),
                ("PORT", "port"),
                ("DB", "dbname"),
                ("USER", "user"),
                ("PASSWORD", "password"),
            )
        },
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_dbt.py"),
            "build",
            "--schema",
            schema,
            "--vars",
            json.dumps(variables),
            "--target-path",
            str(artifacts),
            "--log-path",
            str(artifacts / "logs"),
            "--no-partial-parse",
            "--warn-error",
        ],
        cwd=root,
        env=environment,
        check=False,
        timeout=900,
    )

    if completed.returncode:
        raise ValueError(f"dbt build failed; inspect {artifacts}")

    return checked_results(artifacts)


def activate(conn, publication_id, source_id):
    conn.execute(
        """
        INSERT INTO serving.current_publications(source_id, publication_id)
        VALUES (%s, %s)
        ON CONFLICT (source_id) DO UPDATE
            SET publication_id = EXCLUDED.publication_id,
                activated_at = clock_timestamp()
        WHERE current_publications.publication_id <> EXCLUDED.publication_id
        """,
        (source_id, publication_id),
    )


def promote(conn, metadata, attempt_id, schema, code_hash, status):
    current = conn.execute(
        """
        SELECT p.publication_id, p.release_id
        FROM serving.current_publications c
        JOIN serving.publications p USING (publication_id)
        WHERE c.source_id = %s
        """,
        (metadata["source_id"],),
    ).fetchone()

    if current and current["release_id"] > metadata["release_id"]:
        raise ValueError("An earlier release requires an explicit restore")

    relation = sql.Identifier(schema + "_staging", "stg_nc_releases")
    candidate = conn.execute(
        sql.SQL("SELECT release_id, source_id FROM {}").format(relation)
    ).fetchall()

    if candidate != [{
        "release_id": metadata["release_id"],
        "source_id": metadata["source_id"],
    }]:
        raise ValueError("Candidate must contain exactly the requested release")

    existing = conn.execute(
        """
        SELECT publication_id FROM serving.publications
        WHERE source_id = %s AND release_id = %s
          AND transform_sha256 = %s AND status = %s
        """,
        (
            metadata["source_id"],
            metadata["release_id"],
            code_hash,
            status,
        ),
    ).fetchone()

    publication_id = existing["publication_id"] if existing else attempt_id

    if not existing:
        fields = (
            "source_id release_id snapshot_id state election_date dataset "
            "source_url raw_sha256 first_observed_at source_modified_at"
        ).split()

        conn.execute(
            sql.SQL("""
                INSERT INTO serving.publications
                    (publication_id, transform_sha256, status, {})
                VALUES (%s, %s, %s, {})
            """).format(
                sql.SQL(", ").join(map(sql.Identifier, fields)),
                sql.SQL(", ").join(sql.Placeholder() for _ in fields),
            ),
            [
                publication_id,
                code_hash,
                status,
                *[metadata[f] for f in fields],
            ],
        )

        for layer, model, target, columns in TARGETS[metadata["dataset"]]:
            identifiers = sql.SQL(", ").join(
                map(sql.Identifier, columns.split())
            )
            count = conn.execute(
                sql.SQL("""
                    INSERT INTO serving.{} (publication_id, {})
                    SELECT %s, {} FROM {} WHERE release_id = %s
                """).format(
                    sql.Identifier(target),
                    identifiers,
                    identifiers,
                    sql.Identifier(schema + "_" + layer, model),
                ),
                (publication_id, metadata["release_id"]),
            ).rowcount

            if count == 0:
                raise ValueError(f"No rows copied from {model}")

    activate(conn, publication_id, metadata["source_id"])
    outcome = (
        "skipped"
        if current and current["publication_id"] == publication_id
        else "published"
    )

    conn.execute(
        """
        UPDATE ops.publication_attempts
        SET outcome = %s, publication_id = %s, previous_publication_id = %s,
            finished_at = clock_timestamp()
        WHERE attempt_id = %s
        """,
        (
            outcome,
            publication_id,
            current["publication_id"] if current else None,
            attempt_id,
        ),
    )

    return {"outcome": outcome, "publication_id": str(publication_id)}


def publish(root, db, release_id, status, *, skip_current=False):
    if status not in ("test", "unofficial"):
        raise ValueError("Status must be test or unofficial")

    root = root.resolve()
    attempt_id = uuid.uuid4()
    schema = "publish_" + attempt_id.hex
    artifacts = root / "data/publications" / str(attempt_id)

    with connect(db) as conn:
        # Session lock covers both dbt and the final publication transaction.
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(20261103, 1) AS ok"
        ).fetchone()["ok"]
        if not locked:
            raise ValueError("Another publication or restore is already running")

        metadata = release_metadata(conn, release_id)
        code_hash = project_hash(root)
        if skip_current:
            current = conn.execute(
                """
                SELECT p.publication_id
                FROM serving.current_publications c
                JOIN serving.publications p USING (publication_id)
                WHERE c.source_id = %s
                  AND p.release_id = %s
                  AND p.transform_sha256 = %s
                  AND p.status = %s
                """,
                (metadata["source_id"], release_id, code_hash, status),
            ).fetchone()

            if current:
                return {
                    "outcome": "already_current",
                    "publication_id": str(current["publication_id"]),
                    "release_id": release_id,
                }

        conn.execute(
            """
            INSERT INTO ops.publication_attempts
                (attempt_id, release_id, action, artifacts_path)
            VALUES (%s, %s, 'publish', %s)
            """,
            (attempt_id, release_id, str(artifacts)),
        )

        try:
            results = build_candidate(root, db, metadata, schema, artifacts)

            if code_hash != project_hash(root):
                raise ValueError(
                    "Project files changed during the candidate build; retry"
                )

            with conn.transaction():
                conn.execute(
                    """
                    UPDATE ops.publication_attempts
                    SET run_results = %s WHERE attempt_id = %s
                    """,
                    (Jsonb(results), attempt_id),
                )
                result = promote(
                    conn, metadata, attempt_id, schema, code_hash, status
                )

            return {
                **result,
                "attempt_id": str(attempt_id),
                "release_id": release_id,
            }

        except Exception as exc:
            conn.execute(
                """
                UPDATE ops.publication_attempts
                SET outcome = 'failed', finished_at = clock_timestamp(),
                    error_message = %s
                WHERE attempt_id = %s
                """,
                (
                    str(exc).replace(db["password"], "[redacted]")[:4000],
                    attempt_id,
                ),
            )
            raise

        finally:
            for layer in ("marts", "staging"):
                try:
                    conn.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            sql.Identifier(schema + "_" + layer)
                        )
                    )
                except Exception as exc:
                    print(
                        "Candidate cleanup needs attention: "
                        + str(exc).replace(db["password"], "[redacted]"),
                        file=sys.stderr,
                    )


def restore(db, publication_id):
    """Intentionally reactivate an already validated, frozen publication."""
    with connect(db) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(20261103, 1) AS ok"
        ).fetchone()["ok"]
        if not locked:
            raise ValueError("Another publication or restore is already running")

        publication = conn.execute(
            """
            SELECT * FROM serving.publications
            WHERE publication_id = %s
            """,
            (publication_id,),
        ).fetchone()

        if not publication:
            raise ValueError("Unknown publication ID")

        attempt_id = uuid.uuid4()

        with conn.transaction():
            current = conn.execute(
                """
                SELECT publication_id FROM serving.current_publications
                WHERE source_id = %s
                """,
                (publication["source_id"],),
            ).fetchone()

            activate(conn, publication_id, publication["source_id"])
            conn.execute(
                """
                INSERT INTO ops.publication_attempts
                    (attempt_id, release_id, action, outcome, publication_id,
                     previous_publication_id, finished_at)
                VALUES (
                    %s, %s, 'restore', 'published', %s, %s, clock_timestamp()
                )
                """,
                (
                    attempt_id,
                    publication["release_id"],
                    publication_id,
                    current["publication_id"] if current else None,
                ),
            )

        return {
            "outcome": "restored",
            "publication_id": str(publication_id),
        }