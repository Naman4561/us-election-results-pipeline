"""Shared storage contract for total-only feeds. Unknown votes remain SQL NULL."""

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


def load_contract(dataset):
    from election_pipeline.states import LoadContract

    if dataset != "election_results":
        raise ValueError("Only election results are supported")
    return LoadContract(
        "results",
        "results.csv",
        FIELDS,
        ("results.csv", "county_results.csv", "contest_results.csv"),
        "bundle",
    )


def database_row(row, source, release):
    from election_pipeline.database import number

    numeric = {"source_line", "total_votes", "precincts_reporting", "precincts_total"}
    return (release,) + tuple(
        (number(row[k]) if row[k] else None)
        if k in numeric
        else (row[k] or None)
        if k == "choice_party"
        else row[k]
        for k in FIELDS
        if k not in {"state", "election_date"}
    )


def finish(rows, source, settings, output, stats, *, reference=None, reconcile=False):
    from election_pipeline.parsing import reference_check, write_groups
    from election_pipeline.states import state_adapter

    adapter = state_adapter(source.state)
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
            state[pair] = [row["total_votes"]]
        elif row["unit_type"] == "county":
            county[(source.election_date, source.state, row["county"], *pair)] = [
                row["total_votes"]
            ]
            coverage[row["contest_id"]].add(row["county"])
        else:
            raise ValueError("Unsupported geography level")
    if {k[0] for k in state} != set(adapter.races):
        raise ValueError("Selected contest coverage is incomplete")
    for contest in adapter.races:
        votes = [v[0] for k, v in state.items() if k[0] == contest]
        if len(votes) < 2 or (any(v is None for v in votes) and any(v is not None for v in votes)):
            raise ValueError("Incomplete candidate counts; mixed blank and numeric results")
    if reconcile:
        calculated = {}
        for key, values in county.items():
            target = calculated.setdefault(key[3:], [0])
            target[0] += values[0]
        if state != calculated:
            raise ValueError("County and official contest candidate totals disagree")
    with (output / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_groups(
        output / "county_results.csv",
        county,
        ("election_date", "state", "county", "contest_id", "choice", "choice_party"),
        ("total_votes",),
    )
    official = {(source.election_date, source.state, *k): v for k, v in state.items()}
    write_groups(
        output / "contest_results.csv",
        official,
        ("election_date", "state", "contest_id", "choice", "choice_party"),
        ("total_votes",),
    )
    known = all(v[0] is not None for v in state.values())
    if reference is not None and not known:
        raise ValueError("Cannot compare unknown counts with a numerical reference")
    stats.update(
        output_rows=len(rows),
        internal_reconciliation="passed",
        county_reconciliation="passed" if reconcile else "not_available",
        counts_available=known,
        all_counts_zero=known and not any(v[0] for v in state.values()),
        contest_count=len(adapter.races),
        county_counts={k: len(v) for k, v in coverage.items()},
        reference_comparison=reference_check(reference, source, official, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided"},
        warnings=(
            ["County detail and vote-method breakdowns are unavailable in this adapter."]
            if not adapter.county_results
            else ["Vote-method breakdowns are unavailable."]
        )
        + ([] if known else ["Vote cells are blank; counts remain unknown, not zero."]),
        election_status="not determined by parser",
    )
