"""Copy the per-state result tables (migrations 003-023) into the shared ones (024) once.

Each schema is copied in its own transaction and checked before commit: per release
and per publication, row counts and vote sums (overall and per method) must equal the
original. A schema already copied is only checked again, so a rerun is safe. The
originals are never modified.
"""

from psycopg import sql

from election_pipeline.recovery import CONSOLIDATED_SCHEMAS

SHARED = (
    "release_id",
    "source_line",
    "unit_type",
    "county",
    "precinct",
    "contest_id",
    "source_contest_id",
    "source_candidate_id",
    "choice",
    "choice_party",
    "total_votes",
    "precincts_reporting",
    "precincts_total",
)
METHODS = (
    "election_day",
    "early_voting",
    "absentee_by_mail",
    "absentee_combined",
    "provisional",
    "post_election",
    "mail",
    "in_person",
)
# North Carolina's copy identified a contest by its group ID and a candidate by name.
FALLBACKS = {
    "unit_type": ("'precinct'",),
    "precinct": ("''",),
    "source_contest_id": ("source_contest_group_id", "contest_id"),
    "source_candidate_id": ("choice",),
    "precincts_reporting": ("NULL::bigint",),
    "precincts_total": ("NULL::bigint",),
}


def columns(conn, schema, table):
    return [
        row["column_name"]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
    ]


def expression(name, present):
    if name in present:
        return sql.Identifier(name)
    for fallback in FALLBACKS[name]:
        if fallback in present:
            return sql.Identifier(fallback)
        if not fallback.isidentifier():
            return sql.SQL(fallback)
    raise ValueError(f"No source for {name}")


def copy_landing(conn, schema):
    present = columns(conn, schema, "results")
    methods = [m for m in METHODS if m in present]
    extras = [c for c in present if c not in SHARED and c not in methods]
    source = sql.Identifier(schema, "results")
    conn.execute(
        sql.SQL(
            "INSERT INTO landing.results ({}, state, attributes) "
            "SELECT {}, %s, jsonb_strip_nulls(jsonb_build_object({})) FROM {}"
        ).format(
            sql.SQL(", ").join(map(sql.Identifier, SHARED)),
            sql.SQL(", ").join(expression(c, present) for c in SHARED),
            sql.SQL(", ").join(
                sql.SQL("{}, {}").format(sql.Literal(c), sql.Identifier(c)) for c in extras
            ),
            source,
        ),
        (schema.upper(),),
    )
    for method in methods:
        conn.execute(
            sql.SQL(
                "INSERT INTO landing.result_methods (release_id, source_line, method, votes) "
                "SELECT release_id, source_line, %s, {} FROM {} WHERE {} IS NOT NULL"
            ).format(sql.Identifier(method), source, sql.Identifier(method)),
            (method,),
        )
    if "reporting_units" in tables(conn, schema):
        conn.execute(
            sql.SQL(
                "INSERT INTO landing.reporting_units (release_id, source_line, state, unit_type,"
                " county, precinct, source_unit_id, reporting_status, precincts_participating,"
                " precincts_reporting, ballots_cast, registered_voters) "
                "SELECT release_id, source_line, %s, unit_type, county, precinct,"
                " reporting_unit_id, reporting_status, precincts_participating,"
                " precincts_reporting, ballots_cast, registered_voters FROM {}"
            ).format(sql.Identifier(schema, "reporting_units")),
            (schema.upper(),),
        )
    return methods


def copy_serving(conn, schema, kind, methods):
    place = ["county"] if kind == "county" else []
    measures = [] if kind == "county" else ["contest_votes", "vote_share"]
    original = sql.Identifier(schema, f"{kind}_candidate_results")
    keys = ["publication_id", *place, "contest_id", "choice"]
    shared = [*keys, "choice_party", "total_votes", *measures]
    conn.execute(
        sql.SQL("INSERT INTO {} (state, {}) SELECT %s, {} FROM {}").format(
            sql.Identifier("serving", f"{kind}_results"),
            sql.SQL(", ").join(map(sql.Identifier, shared)),
            sql.SQL(", ").join(map(sql.Identifier, shared)),
            original,
        ),
        (schema.upper(),),
    )
    for method in methods:
        conn.execute(
            sql.SQL("INSERT INTO {} ({}, method, votes) SELECT {}, %s, {} FROM {}").format(
                sql.Identifier("serving", f"{kind}_method_results"),
                sql.SQL(", ").join(map(sql.Identifier, keys)),
                sql.SQL(", ").join(map(sql.Identifier, keys)),
                sql.Identifier(method),
                original,
            ),
            (method,),
        )


def tables(conn, schema):
    return {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema,)
        )
    }


def summary(conn, query):
    return {tuple(row.values())[0]: tuple(row.values())[1:] for row in conn.execute(query)}


def check(conn, schema, methods, kinds):
    """Per original release / publication: rows and vote sums, overall and per method.

    Only identities present in the original tables are compared; releases loaded after
    cutover exist only in the shared tables.
    """

    def compare(label, original, shared):
        if summary(conn, original) != summary(conn, shared):
            raise ValueError(f"{schema}: {label} differ after copying")

    method_sum = (
        sql.SQL(" + ").join(
            sql.SQL("coalesce(sum({}), 0)").format(sql.Identifier(m)) for m in methods
        )
        if methods
        else sql.SQL("0")
    )
    results = sql.Identifier(schema, "results")
    compare(
        "landing rows",
        sql.SQL("SELECT release_id, count(*), sum(total_votes), {} FROM {} GROUP BY 1").format(
            method_sum, results
        ),
        sql.SQL(
            "SELECT r.release_id, count(*), sum(r.total_votes), "
            "coalesce((SELECT sum(m.votes) FROM landing.result_methods m "
            "WHERE m.release_id = r.release_id), 0) "
            "FROM landing.results r WHERE r.release_id IN (SELECT release_id FROM {}) "
            "GROUP BY 1"
        ).format(results),
    )
    if "reporting_units" in tables(conn, schema):
        units = sql.Identifier(schema, "reporting_units")
        compare(
            "reporting units",
            sql.SQL("SELECT release_id, count(*), sum(ballots_cast) FROM {} GROUP BY 1").format(
                units
            ),
            sql.SQL(
                "SELECT release_id, count(*), sum(ballots_cast) FROM landing.reporting_units "
                "WHERE release_id IN (SELECT release_id FROM {}) GROUP BY 1"
            ).format(units),
        )
    for kind in kinds:
        original = sql.Identifier(schema, f"{kind}_candidate_results")
        compare(
            f"{kind} results",
            sql.SQL(
                "SELECT publication_id, count(*), sum(total_votes), {} FROM {} GROUP BY 1"
            ).format(method_sum, original),
            sql.SQL(
                "SELECT r.publication_id, count(*), sum(r.total_votes), "
                "coalesce((SELECT sum(m.votes) FROM {} m "
                "WHERE m.publication_id = r.publication_id), 0) "
                "FROM {} r WHERE r.publication_id IN (SELECT publication_id FROM {}) "
                "GROUP BY 1"
            ).format(
                sql.Identifier("serving", f"{kind}_method_results"),
                sql.Identifier("serving", f"{kind}_results"),
                original,
            ),
        )


def consolidate(conn, schemas=CONSOLIDATED_SCHEMAS + ("ms", "nc")):
    report = {}
    for schema in schemas:
        present = tables(conn, schema)
        if "results" not in present:
            report[schema] = "absent"
            continue
        kinds = [k for k in ("contest", "county") if f"{k}_candidate_results" in present]
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = 0")
            conn.execute("LOCK TABLE landing.results IN SHARE ROW EXCLUSIVE MODE")
            methods = [m for m in METHODS if m in columns(conn, schema, "results")]
            copied = conn.execute(
                sql.SQL(
                    "SELECT EXISTS (SELECT 1 FROM landing.results "
                    "WHERE release_id IN (SELECT release_id FROM {})) AS done"
                ).format(sql.Identifier(schema, "results"))
            ).fetchone()["done"]
            if not copied:
                copy_landing(conn, schema)
                for kind in kinds:
                    copy_serving(conn, schema, kind, methods)
            check(conn, schema, methods, kinds)
        report[schema] = "verified" if copied else "copied"
    return report
