"""Validated filters, stable sorting, and summaries over an immutable table."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from election_pipeline.research.catalog import Catalog, typed


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(max_length=160)
    operator: Literal["eq", "ne", "contains", "gte", "lte", "missing", "present"]
    value: str = Field(default="", max_length=4096)


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: list[Filter] = Field(default_factory=list, max_length=12)
    sort: str | None = Field(default=None, max_length=160)
    direction: Literal["asc", "desc"] = "asc"
    statistic: str | None = Field(default=None, max_length=160)
    offset: int = Field(default=0, ge=0, le=2**53 - 1)
    limit: int = Field(default=25, ge=1, le=100)


def filters_sql(columns, filters):
    predicates, parameters = [], []
    comparisons = {"eq": "=", "ne": "<>", "gte": ">=", "lte": "<="}
    for rule in filters:
        sql_column, kind = columns[rule.column]
        if rule.operator in {"missing", "present"}:
            test = "IS NULL" if rule.operator == "missing" else "IS NOT NULL"
            predicates.append(f"{sql_column} {test}")
            continue
        if rule.operator == "contains":
            if kind != "text":
                raise ValueError("Contains requires a text column")
            predicates.append(f"instr(lower({sql_column}), lower(?)) > 0")
            parameters.append(rule.value)
            continue
        predicates.append(f"{sql_column} {comparisons[rule.operator]} ?")
        parameters.append(typed(rule.value, kind))
    return (" WHERE " + " AND ".join(predicates) if predicates else ""), parameters


def summarize(db, table, where, parameters, column, kind):
    # Use the same predicate as the result page. NULL values are counted separately.
    expressions = f"COUNT({column}) AS present, COUNT(*) - COUNT({column}) AS missing"
    if kind == "text":
        row = db.execute(
            f"SELECT {expressions}, COUNT(DISTINCT {column}) AS distinct_values "
            f"FROM {table}{where}",
            parameters,
        ).fetchone()
        non_null = (where + " AND " if where else " WHERE ") + f"{column} IS NOT NULL"
        common = db.execute(
            f"""SELECT {column} AS value, COUNT(*) AS count FROM {table}{non_null}
                GROUP BY {column} ORDER BY count DESC, value LIMIT 10""",
            parameters,
        )
        return {**dict(row), "most_common": [dict(item) for item in common]}

    row = db.execute(
        f"""SELECT {expressions}, SUM(CAST({column} AS REAL)) AS sum,
                   AVG({column}) AS mean, MIN({column}) AS min, MAX({column}) AS max
            FROM {table}{where}""",
        parameters,
    ).fetchone()
    return {
        key: None if isinstance(value, float) and not math.isfinite(value) else value
        for key, value in dict(row).items()
    }


def explore(catalog: Catalog, version_id: int, query: Query) -> dict:
    version = catalog.version(version_id)
    if not version["columns"]:
        raise ValueError("This version contains files only. Import a CSV or TSV to analyze it")
    columns = {
        column["name"]: (f"c{i}", column["type"]) for i, column in enumerate(version["columns"])
    }
    names = [rule.column for rule in query.filters]
    names.extend(name for name in (query.sort, query.statistic) if name is not None)
    if missing := set(names) - columns.keys():
        raise ValueError(f"Unknown columns: {', '.join(sorted(missing))}")

    where, parameters = filters_sql(columns, query.filters)
    table = f"rows_{version['id']}"
    order = "rowid"
    if query.sort is not None:
        column, _ = columns[query.sort]
        order = f"{column} IS NULL, {column} {query.direction.upper()}, rowid"

    with catalog.connect() as db:
        total = db.execute(f"SELECT COUNT(*) FROM {table}{where}", parameters).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM {table}{where} ORDER BY {order} LIMIT ? OFFSET ?",
            (*parameters, query.limit, query.offset),
        )
        result = {"rows": [list(row) for row in rows], "matching_rows": total, "statistics": None}
        if query.statistic is not None:
            column, kind = columns[query.statistic]
            result["statistics"] = summarize(db, table, where, parameters, column, kind)
        return result
