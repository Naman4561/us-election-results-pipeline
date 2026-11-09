"""The one parser output layout shared by every state. Unknown votes remain blank (NULL).

A parser emits one row per candidate per geography the source publishes: statewide,
county or precinct rows, or several of these. ``finish`` checks them, derives county and
statewide totals the source does not print, and writes:

* ``results.csv``: the source rows, loaded into ``landing.results``. A state that publishes
  how votes were cast (registry ``State.methods``) adds one column per method; each row's
  methods must then all be known and add up exactly to its ``total_votes``.
* ``county_results.csv`` and ``contest_results.csv``: candidate totals per county and per
  contest, used for reference comparisons and accuracy checks.
* ``reporting_units.csv``, when the source reports turnout per reporting unit.
"""

import csv
import json
from collections import defaultdict

FIELDS = (
    "source_line",
    "election_date",
    "state",
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
    "attributes",
)
UNIT_FIELDS = (
    "source_line",
    "election_date",
    "state",
    "unit_type",
    "county",
    "precinct",
    "source_unit_id",
    "reporting_status",
    "precincts_participating",
    "precincts_reporting",
    "ballots_cast",
    "registered_voters",
)
FILES = ("results.csv", "county_results.csv", "contest_results.csv")
UNITS = ("state", "county", "precinct")


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


def add(groups, key, values):
    """Sum measure lists; an unknown (None) count makes the sum unknown."""
    if key not in groups:
        groups[key] = list(values)
        return
    groups[key] = [None if a is None or b is None else a + b for a, b in zip(groups[key], values)]


def write_groups(path, groups, dimensions, measures):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((*dimensions, *measures))
        for key, values in sorted(groups.items(), key=lambda item: [str(k) for k in item[0]]):
            writer.writerow((*key, *("" if v is None else v for v in values)))


def finish(rows, source, settings, output, stats, *, reference=None, reconcile=False, units=()):
    """Check rows against the state's races and methods; write the shared outputs.

    ``reconcile``: the source publishes statewide and county rows that must agree exactly.
    Without it, published statewide rows are authoritative (county detail may be partial
    or privacy-masked), and a contest without statewide rows is summed from its counties.
    """
    from election_pipeline.parsing import reference_check
    from election_pipeline.states import state_adapter

    adapter = state_adapter(source.state)
    methods = adapter.methods[1:]
    wanted = adapter.races
    keys, names, reported, county, precinct = set(), set(), {}, {}, {}
    coverage = defaultdict(set)
    if not rows or len(rows) > settings.max_csv_rows:
        raise ValueError("Missing/excessive results")
    for index, row in enumerate(rows, 2):
        row.update(source_line=index, state=source.state, election_date=source.election_date)
        row.setdefault("precinct", "")
        row["attributes"] = json.dumps(row.get("attributes") or {}, sort_keys=True)
        if not row["choice"] or not row["source_candidate_id"]:
            raise ValueError("Missing candidate identity")
        if row["contest_id"] not in wanted:
            raise ValueError(f"Unregistered contest: {row['contest_id']}")
        unit = row["unit_type"]
        if unit not in UNITS or (unit == "state") != (not row["county"]):
            raise ValueError("Unsupported geography level")
        if (unit == "precinct") != bool(row["precinct"]):
            raise ValueError("Precinct rows (and only they) name a precinct")
        place = (unit, row["county"], row["precinct"], row["contest_id"])
        if (*place, row["source_candidate_id"]) in keys:
            raise ValueError("Duplicate candidate result")
        keys.add((*place, row["source_candidate_id"]))
        if (*place, row["choice"]) in names:
            raise ValueError("Duplicate candidate name in geographic contest")
        names.add((*place, row["choice"]))
        pair = (row["contest_id"], row["choice"], row["choice_party"])
        values = counts(row, methods)
        if unit == "state":
            if pair in reported:
                raise ValueError("Duplicate candidate name/party in contest")
            reported[pair] = values
        elif unit == "county":
            add(county, (row["county"], *pair), values)
            coverage[row["contest_id"]].add(row["county"])
        else:
            add(precinct, (row["county"], *pair), values)
    # Published county rows are authoritative; precincts give county totals only for a
    # contest the source does not report per county.
    by_county = {key[1] for key in county}
    for key, values in precinct.items():
        if key[1] not in by_county:
            county[key] = values
            coverage[key[1]].add(key[0])
    derived = {}
    for (_, *pair), values in county.items():
        add(derived, tuple(pair), values)
    if reconcile and reported != derived:
        raise ValueError("County and official contest candidate totals disagree")
    statewide = dict(reported)
    published = {pair[0] for pair in reported}
    statewide.update({k: v for k, v in derived.items() if k[0] not in published})
    if {k[0] for k in statewide} != set(wanted):
        raise ValueError("Contest coverage is incomplete")
    for contest in wanted:
        # Every seat is covered, so an uncontested race legitimately has one candidate.
        votes = [v[-1] for k, v in statewide.items() if k[0] == contest]
        if not votes or (any(v is None for v in votes) and any(v is not None for v in votes)):
            raise ValueError("Incomplete candidate counts; mixed blank and numeric results")
    with (output / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS + methods, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
    measures = (*methods, "total_votes")
    write_groups(
        output / "county_results.csv",
        {(source.election_date, source.state, *k): v for k, v in county.items()},
        ("election_date", "state", "county", "contest_id", "choice", "choice_party"),
        measures,
    )
    official = {(source.election_date, source.state, *k): v for k, v in statewide.items()}
    write_groups(
        output / "contest_results.csv",
        official,
        ("election_date", "state", "contest_id", "choice", "choice_party"),
        measures,
    )
    if units:
        write_units(units, source, output)
    known = all(v[-1] is not None for v in statewide.values())
    if reference is not None and not known:
        raise ValueError("Cannot compare unknown counts with a numerical reference")
    warnings = []
    if not adapter.county_results:
        warnings.append("County detail is unavailable in this adapter.")
    if not methods:
        warnings.append("Vote-method breakdowns are unavailable.")
    if not known:
        warnings.append("Vote cells are blank; counts remain unknown, not zero.")
    stats.update(
        output_rows=len(rows),
        internal_reconciliation="passed",
        county_reconciliation="passed" if reconcile else "not_available",
        counts_available=known,
        all_counts_zero=known and not any(v[-1] for v in statewide.values()),
        contest_count=len(wanted),
        contests=sorted(wanted),
        county_counts={k: len(v) for k, v in coverage.items()},
        reference_comparison=reference_check(reference, source, official, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided"},
        warnings=warnings,
        election_status="not determined by parser",
        **({"vote_methods": list(methods)} if methods else {}),
        **({"reporting_unit_rows": len(units)} if units else {}),
    )


def write_units(units, source, output):
    seen = set()
    for index, unit in enumerate(units, 2):
        unit.update(source_line=index, state=source.state, election_date=source.election_date)
        unit.setdefault("precinct", "")
        if unit["source_unit_id"] in seen:
            raise ValueError("Duplicate reporting unit")
        seen.add(unit["source_unit_id"])
    with (output / "reporting_units.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, UNIT_FIELDS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(units)
