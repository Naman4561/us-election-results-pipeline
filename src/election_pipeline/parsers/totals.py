"""Shared storage contract for total-only feeds. Unknown votes remain SQL NULL.

``with_methods`` extends the same contract with vote-method columns for a state that
publishes them (early, election day, mail ...). Every method count must then be known
and the methods must add up exactly to ``total_votes`` on every row.
"""

import csv
from collections import defaultdict

FIELDS = (
    "source_line",
    "election_date",
    "state",
    "unit_type",
    "county",
    "contest_id",
    "source_contest_id",
    "source_candidate_id",
    "choice",
    "choice_party",
    "total_votes",
    "precincts_reporting",
    "precincts_total",
)


def _contract(fields):
    from election_pipeline.states import LoadContract

    return LoadContract(
        "results",
        "results.csv",
        fields,
        ("results.csv", "county_results.csv", "contest_results.csv"),
        "bundle",
    )


def load_contract(dataset):
    if dataset != "election_results":
        raise ValueError("Only election results are supported")
    return _contract(FIELDS)


def _database_row(fields, row, release):
    from election_pipeline.database import number

    numeric = {"source_line", "total_votes", "precincts_reporting", "precincts_total"}
    numeric |= set(fields[len(FIELDS) :])
    return (release,) + tuple(
        (number(row[k]) if row[k] else None)
        if k in numeric
        else (row[k] or None)
        if k == "choice_party"
        else row[k]
        for k in fields
        if k not in {"state", "election_date"}
    )


def database_row(row, source, release):
    return _database_row(FIELDS, row, release)


def with_methods(methods):
    """(fields, load_contract, database_row) for a feed that also stores vote methods."""
    if not methods or len(set(methods)) != len(methods) or set(methods) & set(FIELDS):
        raise ValueError("Vote methods must be new, distinct columns")
    fields = FIELDS + tuple(methods)

    def method_contract(dataset):
        if dataset != "election_results":
            raise ValueError("Only election results are supported")
        return _contract(fields)

    def method_row(row, source, release):
        return _database_row(fields, row, release)

    return fields, method_contract, method_row


def counts(row, methods):
    """Measures for one row: methods first, total last (reference checks read the last)."""
    total = row["total_votes"]
    if not methods:
        return [total]
    values = [row[m] for m in methods]
    if any(type(v) is not int or v < 0 for v in [*values, total]):
        raise ValueError("Vote-method counts must all be known nonnegative integers")
    if sum(values) != total:
        raise ValueError("Vote methods do not add up to the candidate total")
    return [*values, total]


def finish(rows, source, settings, output, stats, *, reference=None, reconcile=False, methods=()):
    from election_pipeline.parsing import reference_check, write_groups
    from election_pipeline.states import state_adapter

    adapter = state_adapter(source.state)
    if ("total_votes", *methods) != adapter.methods:
        raise ValueError("Stored vote methods differ from the state's published contract")
    wanted = adapter.selected_races(source)
    keys, names, state, county, coverage = set(), set(), {}, {}, defaultdict(set)
    if not rows or len(rows) > settings.max_csv_rows:
        raise ValueError("Missing/excessive selected results")
    for index, row in enumerate(rows, 2):
        row.update(source_line=index, state=source.state, election_date=source.election_date)
        if not row["choice"] or not row["source_candidate_id"]:
            raise ValueError("Missing candidate identity")
        identity = (row["unit_type"], row["county"], row["contest_id"], row["source_candidate_id"])
        if identity in keys:
            raise ValueError("Duplicate candidate result")
        keys.add(identity)
        name_key = (row["unit_type"], row["county"], row["contest_id"], row["choice"])
        if name_key in names:
            raise ValueError("Duplicate candidate name in geographic contest")
        names.add(name_key)
        pair = (row["contest_id"], row["choice"], row["choice_party"])
        if row["unit_type"] == "state":
            if pair in state:
                raise ValueError("Duplicate candidate name/party in contest")
            state[pair] = counts(row, methods)
        elif row["unit_type"] == "county":
            county[(source.election_date, source.state, row["county"], *pair)] = counts(
                row, methods
            )
            coverage[row["contest_id"]].add(row["county"])
        else:
            raise ValueError("Unsupported geography level")
    if {k[0] for k in state} != set(wanted):
        raise ValueError("Selected contest coverage is incomplete")
    for contest in wanted:
        votes = [v[-1] for k, v in state.items() if k[0] == contest]
        minimum = 1 if source.contests else 2
        if len(votes) < minimum or (
            any(v is None for v in votes) and any(v is not None for v in votes)
        ):
            raise ValueError("Incomplete candidate counts; mixed blank and numeric results")
    if reconcile:
        calculated = {}
        for key, values in county.items():
            target = calculated.setdefault(key[3:], [0] * len(values))
            for index, value in enumerate(values):
                target[index] += value
        if state != calculated:
            raise ValueError("County and official contest candidate totals disagree")
    with (output / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS + tuple(methods))
        writer.writeheader()
        writer.writerows(rows)
    write_groups(
        output / "county_results.csv",
        county,
        ("election_date", "state", "county", "contest_id", "choice", "choice_party"),
        (*methods, "total_votes"),
    )
    official = {(source.election_date, source.state, *k): v for k, v in state.items()}
    write_groups(
        output / "contest_results.csv",
        official,
        ("election_date", "state", "contest_id", "choice", "choice_party"),
        (*methods, "total_votes"),
    )
    known = all(v[-1] is not None for v in state.values())
    if reference is not None and not known:
        raise ValueError("Cannot compare unknown counts with a numerical reference")
    stats.update(
        output_rows=len(rows),
        internal_reconciliation="passed",
        county_reconciliation="passed" if reconcile else "not_available",
        counts_available=known,
        all_counts_zero=known and not any(v[-1] for v in state.values()),
        contest_count=len(wanted),
        contests=sorted(wanted),
        minimum_candidates=1 if source.contests else 2,
        county_counts={k: len(v) for k, v in coverage.items()},
        reference_comparison=reference_check(reference, source, official, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided"},
        warnings=(
            (
                ["County detail is unavailable in this adapter."]
                if methods
                else ["County detail and vote-method breakdowns are unavailable in this adapter."]
            )
            if not adapter.county_results
            else []
            if methods
            else ["Vote-method breakdowns are unavailable."]
        )
        + ([] if known else ["Vote cells are blank; counts remain unknown, not zero."]),
        election_status="not determined by parser",
        **({"vote_methods": list(methods)} if methods else {}),
    )
