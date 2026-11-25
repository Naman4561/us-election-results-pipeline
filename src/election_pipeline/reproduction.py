"""Re-create a live publication from its raw snapshot with the current code, and compare.

The project's central claim is that every published result can be re-created from the
official file it came from. This checks it for one live publication, without writing
anything it keeps:

1. The raw snapshot is parsed again, from scratch, in a scratch archive store (a parse
   is otherwise reused when its inputs are unchanged, which would prove nothing). Its
   output files must have the SHA-256s the publication's release recorded when it was
   loaded.
2. The release is built again with the current dbt project into a private candidate
   schema, and every table the publication froze (``publication.targets``) is compared
   with the candidate, row for row. The candidate is then dropped.

The outcome is ``reproduced``, ``differs`` (with the files, tables and sample rows that
differ, and the code hashes involved) or ``failed`` (an error, reported as is). Status
and attestation are not results and are not compared; nor is Mississippi's county feed
health, which a live publication refreshes in place.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from collections import Counter
from pathlib import Path

from psycopg import sql

from election_pipeline.database import connect
from election_pipeline.parsing import parse_source
from election_pipeline.publication import (
    build_candidate,
    cleanup_candidate,
    project_hash,
    release_metadata,
    targets,
)
from election_pipeline.storage import ArchiveStore, utc_now

# Refreshed in place on a live publication (publication.refresh_county_feeds).
NOT_FROZEN = {("ms", "county_feed_status")}
SAMPLE = 5


def rows(conn, relation, columns, key, value):
    """The rows of ``relation`` where ``key`` is ``value``, as a multiset of tuples."""
    statement = sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.Identifier(*relation),
        sql.Identifier(key),
    )
    return Counter(tuple(r[c] for c in columns) for r in conn.execute(statement, (value,)))


def compared(published, rebuilt):
    """{"published_only": n, "rebuilt_only": n, samples} for two multisets, or None."""
    if published == rebuilt:
        return None
    gone, new = published - rebuilt, rebuilt - published
    return {
        "published_rows": sum(published.values()),
        "rebuilt_rows": sum(rebuilt.values()),
        "published_only": sum(gone.values()),
        "rebuilt_only": sum(new.values()),
        "published_only_sample": [list(map(str, r)) for r in list(gone)[:SAMPLE]],
        "rebuilt_only_sample": [list(map(str, r)) for r in list(new)[:SAMPLE]],
    }


def reproduce(root, source, settings, data_dir, db, publication_id):
    """Re-create one live publication of ``source``; return its report."""
    root, data_dir = Path(root).resolve(), Path(data_dir)
    report = {
        "schema_version": 1,
        "source_id": source.source_id,
        "publication_id": str(publication_id),
        "started_at": utc_now(),
    }
    with connect(db) as conn:
        publication = conn.execute(
            "SELECT * FROM serving.publications WHERE publication_id = %s", (publication_id,)
        ).fetchone()
        if not publication or publication["source_id"] != source.source_id:
            raise ValueError("Not a publication of this source")
        metadata = release_metadata(conn, publication["release_id"])
    recorded = metadata["parser_report"] or {}
    report.update(
        release_id=publication["release_id"],
        raw_sha256=publication["raw_sha256"],
        published_transform_sha256=publication["transform_sha256"],
        current_transform_sha256=project_hash(root, metadata),
        published_parser_code_sha256=(recorded.get("identity") or {}).get("code_sha256"),
    )
    differences = {}

    # 1. Parse the raw snapshot again, from scratch.
    raw = ArchiveStore(data_dir, source).archive_path(publication["raw_sha256"])
    with tempfile.TemporaryDirectory(prefix="reproduce-") as directory:
        scratch = ArchiveStore(Path(directory), source)
        target = scratch.archive_path(publication["raw_sha256"])
        target.parent.mkdir(parents=True)
        shutil.copyfile(raw, target)
        parsed = parse_source(source, settings, Path(directory), sha256=publication["raw_sha256"])
        report["parser_code_sha256"] = (parsed.get("identity") or {}).get("code_sha256")
        if parsed.get("outcome") != "parsed":
            differences["parse"] = {"outcome": parsed.get("outcome"), "error": parsed.get("error")}
        else:
            mine, theirs = parsed["output_sha256"], recorded.get("output_sha256") or {}
            changed = sorted(n for n in set(mine) | set(theirs) if mine.get(n) != theirs.get(n))
            if changed:
                differences["parsed_files"] = changed

    # 2. Build the release again with the current dbt project and compare the tables.
    schema = "r_" + metadata["state"].lower() + "_" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="reproduce-dbt-") as artifacts:
        try:
            build_candidate(root, db, metadata, schema, Path(artifacts))
            tables = {}
            with connect(db) as conn:
                for layer, model, relation, columns in targets(metadata):
                    if relation in NOT_FROZEN:
                        continue
                    names = columns.split()
                    found = compared(
                        rows(conn, relation, names, "publication_id", publication_id),
                        rows(
                            conn,
                            (schema + "_" + layer, model),
                            names,
                            "release_id",
                            publication["release_id"],
                        ),
                    )
                    tables[".".join(relation)] = "identical" if found is None else "differs"
                    if found is not None:
                        differences.setdefault("tables", {})[".".join(relation)] = found
            report["tables"] = tables
        finally:
            with connect(db) as conn:
                cleanup_candidate(conn, schema)

    report.update(
        outcome="differs" if differences else "reproduced",
        finished_at=utc_now(),
        **({"differences": differences} if differences else {}),
    )
    return report
