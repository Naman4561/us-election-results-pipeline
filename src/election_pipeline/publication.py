"""Validate isolated candidate tables and atomically publish frozen outputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from election_pipeline.database import connect
from election_pipeline.states import publication_lock, state_adapter

FLORIDA_SOURCE = "fl_2026_general"
IOWA_SOURCE = "ia_2026_general"
SAME_OUTPUT_KEYS = ("output_sha256", "output_rows", "contests")
DBT_CACHES = ("partial_parse.msgpack", "graph.gpickle")


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


def florida_file_modified_range(conn, snapshot_id, source_url):
    """Collect the bounds of valid timestamps observed for identical file bytes."""
    headers = conn.execute(
        """
        SELECT metadata #>> '{components,0,headers,last-modified}' AS modified,
               min(finished_at) AS first_seen
        FROM ops.fetch_attempts
        WHERE source_id = %s AND snapshot_id = %s AND http_status = 200
          AND outcome IN ('downloaded', 'unchanged') AND finished_at IS NOT NULL
          AND metadata #>> '{components,0,name}' = 'results.txt'
          AND metadata #>> '{components,0,url}' = %s
          AND metadata #>> '{components,0,headers,last-modified}' IS NOT NULL
        GROUP BY 1
        """,
        (FLORIDA_SOURCE, snapshot_id, source_url),
    ).fetchall()
    if not headers:
        return None
    timestamps = []
    for header in headers:
        try:
            modified = parsedate_to_datetime(header["modified"])
        except (ValueError, TypeError, OverflowError):
            return None
        if modified.tzinfo is None or modified > header["first_seen"]:
            return None
        timestamps.append(modified)
    return min(timestamps), max(timestamps)


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

    if row["dataset"] not in state_adapter(row["state"]).datasets:
        raise ValueError("Unsupported dataset for this state")
    if row["source_id"] == FLORIDA_SOURCE:
        modified_range = florida_file_modified_range(conn, row["snapshot_id"], row["source_url"])
        row["source_modified_range"] = modified_range
        row["source_modified_at"] = modified_range[1] if modified_range else None

    return row


def checked_results(path, tags):
    """The dbt run results, if every selected model, test and unit test passed."""
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((path / "run_results.json").read_text(encoding="utf-8"))

    invocation = results["metadata"]["invocation_id"]
    if not invocation or invocation != manifest["metadata"]["invocation_id"]:
        raise ValueError("dbt artifacts do not belong to one invocation")

    def selected(node):
        return bool(set(tags) & set(node.get("tags", [])))

    models = {
        k for k, n in manifest["nodes"].items() if n["resource_type"] == "model" and selected(n)
    }

    def covered(node):
        # dbt runs with --indirect-selection cautious: a test runs only when every model it
        # reads was built in this run.
        parents = {p for p in node.get("depends_on", {}).get("nodes", []) if p.startswith("model.")}
        return bool(parents) and parents <= models

    nodes = {
        k: n
        for k, n in manifest["nodes"].items()
        if k in models or (n["resource_type"] == "test" and covered(n))
    }
    units = {k: n for k, n in manifest.get("unit_tests", {}).items() if covered(n)}
    if any(
        n["resource_type"] == "model" and n["config"]["materialized"] != "table"
        for n in nodes.values()
    ):
        raise ValueError("Publication candidates must be materialized as tables")
    if not models or not any(
        n["resource_type"] == "test" and n.get("name", "").endswith("release_completeness")
        for n in nodes.values()
    ):
        raise ValueError("Required publication checks are missing from the project")
    expected = set(nodes) | set(units)
    actual = {r["unique_id"]: r for r in results["results"]}

    if set(actual) != expected or len(actual) != len(results["results"]):
        raise ValueError("The complete dbt build did not run")

    if any(r["status"] not in ("success", "pass") for r in actual.values()):
        raise ValueError("A dbt node failed, warned, or was skipped")

    return results


def build_tags(metadata):
    if metadata["dataset"] != "election_results":
        return ["demographics"]
    return ["results"] + (["ms"] if state_adapter(metadata["state"]).county_feed_status else [])


def build_variables(metadata):
    """The dbt vars for one release: its state's races, methods and county checks."""
    variables = {
        "publication_materialization": "table",
        "publication_release_id": metadata["release_id"],
        "election_date": str(metadata["election_date"]),
        "state": metadata["state"],
    }
    if metadata["dataset"] == "election_results":
        state = state_adapter(metadata["state"])
        if metadata.get("parser_report", {}).get("contests") != sorted(state.races):
            raise ValueError("Ingestion release has an invalid contest contract")
        variables.update(
            races=sorted(state.races),
            methods=list(state.methods[1:]),
            county_reconciliation=(
                "none"
                if not state.county_results
                else "masked"
                if state.county_privacy_masking
                else "exact"
            ),
        )
    return variables


def build_candidate(root, db, metadata, schema, artifacts):
    variables, tags = build_variables(metadata), build_tags(metadata)

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
            *("tag:" + tag for tag in tags),
            "--indirect-selection",
            "cautious",
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

    results = checked_results(artifacts, tags)
    # dbt's parse caches (about 1 MB per attempt) are never reread: builds use --no-partial-parse.
    for cache in DBT_CACHES:
        (artifacts / cache).unlink(missing_ok=True)
    return results


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


def current_publication(conn, source_id):
    return conn.execute(
        """
        SELECT p.publication_id, p.release_id, p.snapshot_id, p.source_modified_at,
               p.transform_sha256, p.status
        FROM serving.current_publications c
        JOIN serving.publications p USING (publication_id)
        WHERE c.source_id = %s
        """,
        (source_id,),
    ).fetchone()


def require_forward_release(conn, current, metadata):
    if current is None:
        return
    if (
        metadata["source_id"] == FLORIDA_SOURCE
        and current["snapshot_id"] != metadata["snapshot_id"]
    ):
        incoming_range = metadata["source_modified_range"]
        if incoming_range:
            current_range = florida_file_modified_range(
                conn, current["snapshot_id"], metadata["source_url"]
            )
            if current_range:
                if incoming_range[1] < current_range[0]:
                    raise ValueError("An earlier Florida source file requires an explicit restore")
                if incoming_range[0] > current_range[1]:
                    return
        if metadata["snapshot_id"] < current["snapshot_id"]:
            raise ValueError("An earlier release requires an explicit restore")
    if current["release_id"] > metadata["release_id"]:
        if metadata["source_id"] not in (FLORIDA_SOURCE, IOWA_SOURCE) and same_parsed_release(
            conn, metadata, current
        ):
            return
        raise ValueError("An earlier release requires an explicit restore")


def parser_report(conn, release_id):
    return (
        conn.execute(
            "SELECT parser_report FROM ops.processing_releases WHERE release_id=%s",
            (release_id,),
        ).fetchone()["parser_report"]
        or {}
    )


def same_parsed_release(conn, metadata, current):
    """Whether an earlier-numbered release is the live one's data, not older data.

    Some sites serve byte-different copies of one results version (Washington, Nov 8:
    two archives, one update time, identical parsed outputs). Seeing them alternate is
    no rollback; once a code change makes a rebuild due, either copy may be rebuilt.
    """
    report = metadata.get("parser_report") or {}
    if not report.get("output_sha256") or not report.get("source_update_text"):
        return False
    previous = parser_report(conn, current["release_id"])
    return all(
        previous.get(key) == report.get(key) for key in (*SAME_OUTPUT_KEYS, "source_update_text")
    )


def equivalent_iowa_release(conn, current, metadata):
    """Clarity can return distinct bundles of the same Iowa results version."""
    if metadata["source_id"] != IOWA_SOURCE or metadata["snapshot_id"] == current["snapshot_id"]:
        return False
    return conn.execute(
        """
        WITH markers AS (
            SELECT snapshot_id, metadata #>> '{components,0,sha256}' AS marker_sha
            FROM ops.fetch_attempts
            WHERE source_id = %(source_id)s
              AND snapshot_id IN (%(incoming_snapshot)s, %(published_snapshot)s)
              AND http_status = 200 AND outcome IN ('downloaded', 'unchanged')
              AND metadata #>> '{components,0,name}' = 'current_ver.txt'
              AND metadata #>> '{components,0,url}' = %(source_url)s
              AND metadata #>> '{components,0,sha256}' ~ '^[a-f0-9]{64}$'
        ), incoming AS (
            SELECT to_jsonb(r) - 'release_id' - 'source_line' AS result
            FROM landing.results r WHERE release_id = %(incoming_release)s
        ), published AS (
            SELECT to_jsonb(r) - 'release_id' - 'source_line' AS result
            FROM landing.results r WHERE release_id = %(published_release)s
        )
        SELECT
            (SELECT count(DISTINCT snapshot_id) = 2 AND count(DISTINCT marker_sha) = 1
             FROM markers)
            AND EXISTS (
                SELECT 1 FROM ops.processing_releases
                WHERE release_id = %(published_release)s
                  AND parser_report->>'source_update_text' = %(update_text)s
            )
            AND EXISTS (SELECT 1 FROM incoming)
            AND NOT EXISTS (
                (SELECT result FROM incoming EXCEPT ALL SELECT result FROM published)
                UNION ALL
                (SELECT result FROM published EXCEPT ALL SELECT result FROM incoming)
            ) AS equivalent
        """,
        {
            "source_id": IOWA_SOURCE,
            "source_url": metadata["source_url"],
            "incoming_snapshot": metadata["snapshot_id"],
            "published_snapshot": current["snapshot_id"],
            "incoming_release": metadata["release_id"],
            "published_release": current["release_id"],
            "update_text": metadata["parser_report"].get("source_update_text"),
        },
    ).fetchone()["equivalent"]


def promote(conn, metadata, attempt_id, schema, code_hash, status):
    current = current_publication(conn, metadata["source_id"])
    require_forward_release(conn, current, metadata)

    releases = (
        "stg_releases" if metadata["dataset"] == "election_results" else "stg_demographic_releases"
    )
    relation = sql.Identifier(schema + "_staging", releases)
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
                    sql.Identifier(*target),
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
    """(candidate layer, model, serving relation, columns) frozen by a publication."""
    if metadata["dataset"] != "election_results":
        groups = "request_type party_registration sex race ethnicity ballot_count"
        return [
            (
                "staging",
                "stg_nc_absentee_demographics",
                ("nc", "county_absentee_demographics"),
                "county " + groups,
            ),
            (
                "marts",
                "nc_state_absentee_demographics",
                ("nc", "state_absentee_demographics"),
                groups,
            ),
        ]
    adapter = state_adapter(metadata["state"])
    copied = [
        (
            "marts",
            "contest_results",
            ("serving", "contest_results"),
            "state contest_id choice choice_party total_votes contest_votes vote_share",
        )
    ]
    if adapter.methods[1:]:
        copied.append(
            (
                "marts",
                "contest_method_results",
                ("serving", "contest_method_results"),
                "contest_id choice method votes",
            )
        )
    if adapter.county_results:
        copied.append(
            (
                "marts",
                "county_results",
                ("serving", "county_results"),
                "state county contest_id choice choice_party total_votes",
            )
        )
        if adapter.methods[1:]:
            copied.append(
                (
                    "marts",
                    "county_method_results",
                    ("serving", "county_method_results"),
                    "county contest_id choice method votes",
                )
            )
    if adapter.county_feed_status:
        copied.append(
            (
                "marts",
                "ms_county_feed_status",
                ("ms", "county_feed_status"),
                "county county_fips collection_status source_url raw_sha256 last_attempt_at "
                "last_success_at report_generated_at report_date "
                "precincts_reporting precincts_total "
                "absentee_scope affidavit_scope write_in_votes write_in_label issue",
            )
        )
    return copied


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


def same_results(conn, metadata, current):
    """The live publication, if this release parsed to byte-identical outputs.

    Some official files embed their download time, so every collection is a new raw
    snapshot even when no vote changed. Publishing it again would rebuild identical
    tables, so the scheduled runner keeps the live publication instead. The new raw
    snapshot and its release stay recorded; only the redundant publication is skipped.
    """
    report = metadata.get("parser_report") or {}
    if not report.get("output_sha256"):
        return None
    previous = parser_report(conn, current["release_id"])
    if any(previous.get(key) != report.get(key) for key in SAME_OUTPUT_KEYS):
        return None
    return {
        "outcome": "already_current",
        "publication_id": str(current["publication_id"]),
        "release_id": metadata["release_id"],
        "identical_to_release": current["release_id"],
    }


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
            current = current_publication(conn, metadata["source_id"])
            if current and current["transform_sha256"] == code_hash and current["status"] == status:
                if current["release_id"] == release_id or equivalent_iowa_release(
                    conn, current, metadata
                ):
                    return {
                        "outcome": "already_current",
                        "publication_id": str(current["publication_id"]),
                        "release_id": current["release_id"],
                    }
                # Florida needs to advance the published file timestamp. Iowa
                # requires matching Clarity markers and a full loaded-row comparison.
                if metadata["source_id"] not in (FLORIDA_SOURCE, IOWA_SOURCE):
                    same = same_results(conn, metadata, current)
                    if same:
                        return same
            require_forward_release(conn, current, metadata)
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


def discard(db, candidate_id):
    """Drop a validated, unpublished candidate; its attempt is recorded as skipped."""
    with connect(db) as conn, conn.transaction():
        attempt = conn.execute(
            "SELECT * FROM ops.publication_attempts WHERE attempt_id=%s FOR UPDATE",
            (candidate_id,),
        ).fetchone()
        if not attempt or attempt["outcome"] != "running" or not attempt["validated_at"]:
            raise ValueError("Only a validated, unpublished candidate can be discarded")
        cleanup_candidate(conn, attempt["candidate_schema"])
        conn.execute(
            "UPDATE ops.publication_attempts SET outcome='skipped', finished_at=clock_timestamp()"
            " WHERE attempt_id=%s",
            (candidate_id,),
        )
    return {"outcome": "discarded", "candidate_id": str(candidate_id)}


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
