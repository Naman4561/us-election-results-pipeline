"""Apply reviewed, additive DDL for one previously uncovered state."""

import hashlib
import json
import re
from pathlib import Path


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def strings(values):
    return [value["String"]["sval"] for value in values]


def validate_migration(text, schema):
    """An allowlist, not a string search. Existing tables cannot be targets."""
    from pglast.parser import parse_sql_json

    if not re.fullmatch(r"[a-z]{2}", schema) or schema in ("pg",):
        raise ValueError("Expected one new two-letter state schema")
    statements = json.loads(parse_sql_json(text))["stmts"]
    if len(statements) < 3:
        raise ValueError("Empty state migration")
    bodies = [item["stmt"] for item in statements]
    if (
        bodies[0].get("TransactionStmt", {}).get("kind") != "TRANS_STMT_BEGIN"
        or bodies[-1].get("TransactionStmt", {}).get("kind") != "TRANS_STMT_COMMIT"
    ):
        raise ValueError("State migration requires outer BEGIN/COMMIT")
    tables, created = set(), False
    for body in bodies[1:-1]:
        kind, node = next(iter(body.items()))
        if kind == "CreateSchemaStmt":
            if (
                created
                or node.get("schemaname") != schema
                or node.get("authrole")
                or node.get("schemaElts")
                or node.get("if_not_exists")
            ):
                raise ValueError("Migration must create exactly one new state schema")
            created = True
        elif kind == "CreateStmt":
            relation = node["relation"]
            name = relation["relname"]
            if not created or relation.get("schemaname") != schema or name in tables:
                raise ValueError("Tables must belong to the new state schema")
            if (
                any(
                    node.get(key)
                    for key in (
                        "inhRelations",
                        "partbound",
                        "partspec",
                        "ofTypename",
                        "options",
                        "tablespacename",
                        "if_not_exists",
                    )
                )
                or relation.get("relpersistence") != "p"
            ):
                raise ValueError("Only ordinary new tables are permitted")
            tables.add(name)
            for tag, item in walk(node):
                if tag in ("SubLink", "SQLValueFunction", "CollateClause"):
                    raise ValueError("Migration contains an unsupported expression")
                if tag == "FuncCall" and strings(item["funcname"]) not in (["btrim"],):
                    raise ValueError("Only btrim is allowed in state table checks")
                if tag == "TypeName":
                    parts = strings(item["names"])
                    if (
                        parts[-1]
                        not in {"text", "int4", "int8", "numeric", "uuid", "timestamptz", "bool"}
                        or len(parts) > 2
                        or (len(parts) == 2 and parts[0] != "pg_catalog")
                    ):
                        raise ValueError("Only built-in column types are permitted")
                if tag == "pktable":
                    target = (item.get("schemaname"), item["relname"])
                    if target not in {
                        ("ops", "processing_releases"),
                        ("serving", "publications"),
                    } and target not in {(schema, table) for table in tables}:
                        raise ValueError("Foreign key is outside the state publication contract")
        elif kind == "IndexStmt":
            relation = node["relation"]
            if (
                relation.get("schemaname") != schema
                or relation["relname"] not in tables
                or node.get("concurrent")
                or node.get("whereClause")
                or node.get("accessMethod", "btree") != "btree"
                or any(part["IndexElem"].get("expr") for part in node["indexParams"])
            ):
                raise ValueError("Indexes may target only plain columns on newly created tables")
        elif kind == "GrantStmt":
            privileges = {p["AccessPriv"]["priv_name"] for p in node.get("privileges", [])}
            roles = [g["RoleSpec"] for g in node["grantees"]]
            usage = node["objtype"] == "OBJECT_SCHEMA" and node["objects"] == [
                {"String": {"sval": schema}}
            ]
            if node.get("is_grant"):
                # Location is parser metadata, not part of the role contract.
                reader = (
                    len(roles) == 1
                    and roles[0].get("rolename") == "election_dashboard"
                    and roles[0].get("roletype") == "ROLESPEC_CSTRING"
                )
                objects = [p.get("RangeVar", {}) for p in node["objects"]]
                published = node["objtype"] == "OBJECT_TABLE" and all(
                    p.get("schemaname") == schema
                    and p.get("relname") in tables
                    and p["relname"]
                    in {
                        "county_candidate_results",
                        "contest_candidate_results",
                        "county_absentee_demographics",
                        "state_absentee_demographics",
                        "county_feed_status",
                    }
                    for p in objects
                )
                if (
                    not reader
                    or node.get("grant_option")
                    or not (
                        (usage and privileges == {"usage"})
                        or (published and privileges == {"select"})
                    )
                ):
                    raise ValueError("Only reader USAGE and published-table SELECT may be granted")
            elif (
                not usage
                or privileges
                or len(roles) != 1
                or roles[0].get("roletype") != "ROLESPEC_PUBLIC"
            ):
                raise ValueError("Only REVOKE ALL on the new schema from PUBLIC is permitted")
        else:
            raise ValueError(f"Not an additive state migration: {kind}")
    if not created or "contest_candidate_results" not in tables or "results" not in tables:
        raise ValueError("Migration must create raw results and published contest results")
    # Parser offsets avoid stripping SQL with a regex (comments and string literals are legal).
    start = statements[1]["stmt_location"]
    end = statements[-1]["stmt_location"]
    return text.encode("utf-8")[start:end].decode("utf-8")


def apply(conn, path, schema):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    body = validate_migration(text, schema)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout='750ms'")
        conn.execute("SET LOCAL statement_timeout='15s'")
        conn.execute("SET LOCAL search_path=pg_catalog")
        if not conn.execute("SELECT pg_try_advisory_xact_lock(20261105, 1) AS ok").fetchone()["ok"]:
            raise ValueError("Another migration is running; retry later")
        previous = conn.execute(
            "SELECT sha256 FROM ops.schema_migrations WHERE name=%s", (path.name,)
        ).fetchone()
        if previous:
            if previous["sha256"] != digest:
                raise ValueError("Applied migration checksum changed")
            return {"outcome": "already_applied", "migration": path.name}
        if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone():
            raise ValueError("State schema already exists; no live alteration is allowed")
        conn.execute(body)
        conn.execute(
            "INSERT INTO ops.schema_migrations(name,sha256) VALUES (%s,%s)", (path.name, digest)
        )
    return {"outcome": "applied", "migration": path.name}
