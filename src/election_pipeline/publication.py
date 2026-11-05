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
from election_pipeline.states import publication_lock, state_adapter

GROUPS = "request_type party_registration sex race ethnicity ballot_count"


def project_hash(root):
    paths = [
        root / "pyproject.toml",
        root / "scripts/run_dbt.py",
        root / "scripts/publish.py",
        Path(__file__).resolve(),
        root / "src/election_pipeline/states.py",
        root / "src/election_pipeline/database.py",
    ]
    paths += [p for p in (root / "dbt").glob("*.yml") if not p.name.startswith(".")]

    for directory in ("models", "macros", "tests"):
        paths += [
            p
            for p in (root / "dbt" / directory).rglob("*")
            if p.suffix in (".sql", ".yml", ".yaml")
        ]

    digest = hashlib.sha256()

    for package in ("dbt-core", "dbt-postgres", "dbt-adapters", "dbt-common", "psycopg"):
        digest.update(f"{package}={version(package)}".encode() + b"\0")

    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")

    return digest.hexdigest()


def release_metadata(conn, release_id):
    row = conn.execute(
        """
        SELECT r.release_id, r.snapshot_id, r.loaded_at, r.expected_rows,
               r.loaded_rows, r.parser_report, s.source_id, s.state, s.election_date,
               s.dataset, s.source_url, f.raw_sha256,
               (SELECT min(a.finished_at) FROM ops.fetch_attempts a
                WHERE a.snapshot_id = r.snapshot_id
                  AND a.outcome IN ('downloaded', 'manual_imported')) AS first_observed_at,
               (SELECT max(a.source_modified_at) FROM ops.fetch_attempts a
                WHERE a.snapshot_id = r.snapshot_id) AS source_modified_at
        FROM ops.processing_releases r
        JOIN ops.snapshots f USING (snapshot_id)
        JOIN ops.sources s USING (source_id)
        WHERE r.release_id = %s
        """,
        (release_id,),
    ).fetchone()

    if not row or row["loaded_at"] is None or row["loaded_rows"] != row["expected_rows"]:
        raise ValueError("Choose a completed ingestion release")

    state_adapter(row["state"]).contract(row["dataset"])

    return row


def checked_results(path, state=None):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((path / "run_results.json").read_text(encoding="utf-8"))

    invocation = results["metadata"]["invocation_id"]
    if not invocation or invocation != manifest["metadata"]["invocation_id"]:
        raise ValueError("dbt artifacts do not belong to one invocation")

    nodes = manifest["nodes"]
    units = manifest.get("unit_tests", {})
    if state:
        tag = "state_" + state.lower()
        models = {
            k
            for k, n in nodes.items()
            if n["resource_type"] == "model" and tag in n.get("tags", [])
        }
        selected = {
            k: n
            for k, n in nodes.items()
            if k in models
            or (
                n["resource_type"] == "test"
                and (
                    tag in n.get("tags", [])
                    or bool(set(n.get("depends_on", {}).get("nodes", [])) & models)
                )
            )
        }
        units = {
            k: n
            for k, n in units.items()
            if tag in n.get("tags", [])
            or bool(set(n.get("depends_on", {}).get("nodes", [])) & models)
        }
    else:
        selected = nodes
    if any(
        n["resource_type"] == "model" and n["config"]["materialized"] != "table"
        for n in selected.values()
    ):
        raise ValueError("Publication candidates must be materialized as tables")
    required = "release_completeness"
    if not units or not any(
        n["resource_type"] == "test" and n.get("name", "").endswith(required)
        for n in selected.values()
    ):
        raise ValueError("Required publication checks are missing from the project")
    expected = {k for k, n in selected.items() if n["resource_type"] in ("model", "test")}
    expected.update(units)
    actual = {r["unique_id"]: r for r in results["results"]}

    if not expected or set(actual) != expected or len(actual) != len(results["results"]):
        raise ValueError("The complete dbt build did not run")

    if any(r["status"] not in ("success", "pass") for r in actual.values()):
        raise ValueError("A dbt node failed, warned, or was skipped")

    return results


def build_candidate(root, db, metadata, schema, artifacts):
    source_variable = (
        metadata["state"].lower() + "_results_source"
        if metadata["dataset"] == "election_results"
        else metadata["state"].lower() + "_demographics_source"
    )
    variables = {
        "publication_materialization": "table",
        "publication_release_id": metadata["release_id"],
        "election_date": str(metadata["election_date"]),
        source_variable: metadata["source_id"],
    }
    if metadata["dataset"] == "election_results":
        state = state_adapter(metadata["state"])
        races = metadata.get("parser_report", {}).get("contests", list(state.races))
        if (
            not races
            or len(races) != len(set(races))
            or not set(races) <= state.available_races.keys()
        ):
            raise ValueError("Ingestion release has an invalid contest contract")
        variables[state.schema + "_races"] = races
        variables["allow_uncontested"] = (
            metadata.get("parser_report", {}).get("minimum_candidates") == 1
        )

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
            "--select",
            "tag:state_" + metadata["state"].lower(),
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
        stdout=sys.stderr,
        check=False,
        timeout=900,
    )

    if completed.returncode:
        raise ValueError(f"dbt build failed; inspect {artifacts}")

    return checked_results(artifacts, metadata["state"])


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

    relation = sql.Identifier(schema + "_staging", "stg_" + metadata["state"].lower() + "_releases")
    candidate = conn.execute(
        sql.SQL("SELECT release_id, source_id FROM {}").format(relation)
    ).fetchall()

    if candidate != [
        {
            "release_id": metadata["release_id"],
            "source_id": metadata["source_id"],
        }
    ]:
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

        for layer, model, target, columns in targets(metadata):
            identifiers = sql.SQL(", ").join(map(sql.Identifier, columns.split()))
            count = conn.execute(
                sql.SQL("""
                    INSERT INTO {} (publication_id, {})
                    SELECT %s, {} FROM {} WHERE release_id = %s
                """).format(
                    sql.Identifier(metadata["state"].lower(), target),
                    identifiers,
                    identifiers,
                    sql.Identifier(schema + "_" + layer, model),
                ),
                (publication_id, metadata["release_id"]),
            ).rowcount

            if count == 0:
                raise ValueError(f"No rows copied from {model}")

    activate(conn, publication_id, metadata["source_id"])
    outcome = "skipped" if current and current["publication_id"] == publication_id else "published"

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


def targets(metadata):
    adapter = state_adapter(metadata["state"])
    if metadata["dataset"] == "election_results":
        votes = " ".join(adapter.methods)
        county_target = (
            [
                (
                    "marts",
                    adapter.schema + "_county_candidate_results",
                    "county_candidate_results",
                    "county contest_id choice choice_party " + votes,
                ),
            ]
            if adapter.county_results
            else []
        )
        feed_target = (
            [
                (
                    "marts",
                    adapter.schema + "_county_feed_status",
                    "county_feed_status",
                    "county county_fips collection_status source_url raw_sha256 last_attempt_at "
                    "last_success_at report_generated_at report_date "
                    "precincts_reporting precincts_total "
                    "absentee_scope affidavit_scope write_in_votes write_in_label issue",
                )
            ]
            if adapter.county_feed_status
            else []
        )
        return (
            county_target
            + feed_target
            + [
                (
                    "marts",
                    adapter.schema + "_contest_candidate_results",
                    "contest_candidate_results",
                    "contest_id choice choice_party " + votes + " contest_votes vote_share",
                ),
            ]
        )
    return [
        (
            "staging",
            "stg_" + adapter.schema + "_absentee_demographics",
            "county_absentee_demographics",
            "county " + GROUPS,
        ),
        (
            "marts",
            adapter.schema + "_state_absentee_demographics",
            "state_absentee_demographics",
            GROUPS,
        ),
    ]


def cleanup_candidate(conn, schema):
    for layer in ("marts", "staging"):
        conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema + "_" + layer))
        )


def candidate_fingerprints(conn, schema, *, lock=False):
    relations = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = ANY(%s) AND table_type='BASE TABLE' "
        "ORDER BY table_schema, table_name",
        ([schema + "_staging", schema + "_marts"],),
    ).fetchall()
    if not relations:
        raise ValueError("Candidate tables are missing")
    result = {}
    for row in relations:
        relation = sql.Identifier(row["table_schema"], row["table_name"])
        if lock:
            conn.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(relation))
        digest = hashlib.sha256()
        count = 0
        with conn.cursor(name="candidate_" + uuid.uuid4().hex) as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT to_jsonb(t)::text AS value FROM {} t "
                    'ORDER BY to_jsonb(t)::text COLLATE "C"'
                ).format(relation)
            )
            for record in cursor:
                digest.update(record["value"].encode() + b"\n")
                count += 1
        result[row["table_schema"] + "." + row["table_name"]] = {
            "rows": count,
            "sha256": digest.hexdigest(),
        }
    return result


def prepare(root, db, release_id, status, *, skip_current=False):
    """Transform and validate one release; retain an immutable checked checkpoint."""
    if status not in ("test", "unofficial"):
        raise ValueError("Status must be test or unofficial")
    root = root.resolve()
    attempt_id = uuid.uuid4()
    with connect(db) as conn:
        metadata = release_metadata(conn, release_id)
        publication_lock(conn, metadata["state"])
        code_hash = project_hash(root)
        if skip_current:
            current = conn.execute(
                "SELECT p.publication_id FROM serving.current_publications c "
                "JOIN serving.publications p USING (publication_id) "
                "WHERE c.source_id=%s AND p.release_id=%s "
                "AND p.transform_sha256=%s AND p.status=%s",
                (metadata["source_id"], release_id, code_hash, status),
            ).fetchone()
            if current:
                return {
                    "outcome": "already_current",
                    "publication_id": str(current["publication_id"]),
                    "release_id": release_id,
                }
        schema = "p_" + metadata["state"].lower() + "_" + attempt_id.hex
        artifacts = root / "data/publications" / str(attempt_id)
        conn.execute(
            "INSERT INTO ops.publication_attempts "
            "(attempt_id, release_id, action, artifacts_path, candidate_schema, "
            "transform_sha256, requested_status) VALUES (%s,%s,'publish',%s,%s,%s,%s)",
            (attempt_id, release_id, str(artifacts), schema, code_hash, status),
        )
        try:
            results = build_candidate(root, db, metadata, schema, artifacts)
            if code_hash != project_hash(root):
                raise ValueError("Project files changed during the candidate build; retry")
            with conn.transaction():
                fingerprints = candidate_fingerprints(conn, schema, lock=True)
                conn.execute(
                    "UPDATE ops.publication_attempts SET run_results=%s, "
                    "candidate_fingerprints=%s, validated_at=clock_timestamp() WHERE attempt_id=%s",
                    (Jsonb(results), Jsonb(fingerprints), attempt_id),
                )
            return {
                "outcome": "validated",
                "candidate_id": str(attempt_id),
                "release_id": release_id,
            }
        except Exception as exc:
            conn.execute(
                "UPDATE ops.publication_attempts SET outcome='failed', "
                "finished_at=clock_timestamp(), error_message=%s WHERE attempt_id=%s",
                (str(exc).replace(db.get("password", ""), "[redacted]")[:4000], attempt_id),
            )
            cleanup_candidate(conn, schema)
            raise


def publish_prepared(root, db, candidate_id):
    """Promote only the pinned release and table contents which passed validation."""
    with connect(db) as conn:
        attempt = conn.execute(
            "SELECT * FROM ops.publication_attempts WHERE attempt_id=%s", (candidate_id,)
        ).fetchone()
        if not attempt:
            raise ValueError("Unknown candidate ID")
        metadata = release_metadata(conn, attempt["release_id"])
        publication_lock(conn, metadata["state"])
        if attempt["outcome"] in ("published", "skipped"):
            return {
                "outcome": "already_current",
                "publication_id": str(attempt["publication_id"]),
                "release_id": attempt["release_id"],
            }
        if attempt["outcome"] != "running" or not attempt["validated_at"]:
            raise ValueError("Candidate has not passed validation")
        if attempt["transform_sha256"] != project_hash(root.resolve()):
            raise ValueError("Project changed after validation; transform again")
        with conn.transaction():
            actual = candidate_fingerprints(conn, attempt["candidate_schema"], lock=True)
            if actual != attempt["candidate_fingerprints"]:
                raise ValueError("Candidate changed after validation; transform again")
            result = promote(
                conn,
                metadata,
                candidate_id,
                attempt["candidate_schema"],
                attempt["transform_sha256"],
                attempt["requested_status"],
            )
        # Promotion is committed before cleanup; a cleanup failure must not undo publication.
        try:
            cleanup_candidate(conn, attempt["candidate_schema"])
        except Exception:
            print("Published; candidate schema cleanup needs attention", file=sys.stderr)
        return {**result, "attempt_id": str(candidate_id), "release_id": metadata["release_id"]}


def publish(root, db, release_id, status, *, skip_current=False):
    result = prepare(root, db, release_id, status, skip_current=skip_current)
    if result["outcome"] == "already_current":
        return result
    return publish_prepared(root, db, result["candidate_id"])


def restore(db, publication_id):
    """Intentionally reactivate an already validated, frozen publication."""
    with connect(db) as conn:
        publication = conn.execute(
            """
            SELECT * FROM serving.publications
            WHERE publication_id = %s
            """,
            (publication_id,),
        ).fetchone()

        if not publication:
            raise ValueError("Unknown publication ID")

        publication_lock(conn, publication["state"])
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
