"""North Carolina analysis view: results by race, vote methods, count history and
early/absentee ballot demographics.

Reads only published relations through the dashboard's read-only role. Every number is
tied to the publication it came from; candidate votes and ballot demographics are kept
separate because they measure different things.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from threading import Lock

from psycopg import sql

DEMOGRAPHICS_SOURCE = "nc_absentee_early_demographics_2026"
VOTE_METHODS = ("election_day", "early_voting", "absentee_by_mail", "provisional")
DIMENSIONS = ("party_registration", "race", "sex", "ethnicity")
# Publications are created only when the official file changes. 500 points cover a long
# canvass while keeping the response bounded; older points are reported as truncated.
HISTORY_LIMIT = 500
METADATA = (
    "publication_id",
    "source_id",
    "status",
    "published_at",
    "source_modified_at",
    "first_observed_at",
    "source_url",
    "raw_sha256",
    "release_id",
)


def count(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid published count: {label}")
    return value


def checked_methods(row, label):
    votes = {m: count(row[m], label) for m in (*VOTE_METHODS, "total_votes")}
    if sum(votes[m] for m in VOTE_METHODS) != votes["total_votes"]:
        raise ValueError(f"Published vote methods do not reconcile: {label}")
    return votes


def display_name(names, county):
    """Published county text (e.g. NEW HANOVER) to the map's display name."""
    return names.get(re.sub(r"[^A-Z0-9]", "", county.upper()), county.title())


def public_metadata(row):
    if row is None:
        return None
    return {key: (str(row[key]) if key == "publication_id" else row.get(key)) for key in METADATA}


def summarize_race(identity, name, contest_rows, county_rows, county_names, party_group):
    candidates, totals = {}, dict.fromkeys((*VOTE_METHODS, "total_votes"), 0)
    for row in contest_rows:
        key = row["choice"]
        if key in candidates:
            raise ValueError(f"Duplicate published candidate in {identity}")
        votes = checked_methods(row, identity)
        candidates[key] = {
            "name": key,
            "party": row["choice_party"] or None,
            "group": party_group(row["choice_party"], key),
            "votes": votes,
        }
        for method, value in votes.items():
            totals[method] += value
    if not candidates:
        raise ValueError(f"Published snapshot lacks {identity}")
    counties, by_choice = defaultdict(dict), defaultdict(lambda: defaultdict(int))
    for row in county_rows:
        votes = checked_methods(row, identity + " county")
        if row["choice"] not in candidates:
            raise ValueError(f"County result names an unknown candidate in {identity}")
        if row["choice"] in counties[row["county"]]:
            raise ValueError(f"Duplicate county candidate in {identity}")
        counties[row["county"]][row["choice"]] = votes
        for method, value in votes.items():
            by_choice[row["choice"]][method] += value
    if county_rows and any(dict(by_choice[c]) != v["votes"] for c, v in candidates.items()):
        raise ValueError(f"Published county and contest results disagree for {identity}")
    ordered = sorted(candidates.values(), key=lambda c: (-c["votes"]["total_votes"], c["name"]))
    total = totals["total_votes"]
    for candidate in ordered:
        candidate["share"] = candidate["votes"]["total_votes"] / total if total else None
    margin = (
        ordered[0]["votes"]["total_votes"] - ordered[1]["votes"]["total_votes"]
        if len(ordered) > 1 and total
        else None
    )
    return {
        "id": identity,
        "name": name,
        "totals": totals,
        "candidates": ordered,
        "leader": (ordered[0]["group"] if total and margin else None),
        "margin": margin,
        "margin_share": margin / total if margin is not None else None,
        "counties": [
            {
                "county": county,
                "name": display_name(county_names, county),
                "votes": {c: votes for c, votes in sorted(choices.items())},
            }
            for county, choices in sorted(counties.items())
        ],
    }


def summarize_history(points, rows, races):
    by_publication = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        if row["contest_id"] in races:
            by_publication[str(row["publication_id"])][row["contest_id"]][row["choice"]] = (
                checked_methods(row, "history " + row["contest_id"])
            )
    return [
        {
            **public_metadata(point),
            "races": {
                race: {
                    "total": sum(v["total_votes"] for v in choices.values()),
                    "candidates": {name: v["total_votes"] for name, v in choices.items()},
                    "methods": {m: sum(v[m] for v in choices.values()) for m in VOTE_METHODS},
                }
                for race, choices in by_publication[str(point["publication_id"])].items()
            },
        }
        for point in sorted(points, key=lambda p: (p["published_at"], str(p["publication_id"])))
    ]


def summarize_demographics(metadata, state_rows, county_rows, county_names):
    if metadata is None:
        return None
    request_types, dimensions = defaultdict(int), {d: defaultdict(dict) for d in DIMENSIONS}
    for row in state_rows:
        ballots = count(row["ballot_count"], "ballot count")
        kind = row["request_type"]
        request_types[kind] += ballots
        for dimension in DIMENSIONS:
            value = row[dimension] or "Not reported"
            bucket = dimensions[dimension][kind]
            bucket[value] = bucket.get(value, 0) + ballots
    counties = defaultdict(dict)
    for row in county_rows:
        counties[row["county"]][row["request_type"]] = count(row["ballot_count"], "county ballots")
    if county_rows and sum(sum(c.values()) for c in counties.values()) != sum(
        request_types.values()
    ):
        raise ValueError("Published county and statewide ballot demographics disagree")
    return {
        "publication": public_metadata(metadata),
        "total": sum(request_types.values()),
        "request_types": dict(sorted(request_types.items(), key=lambda kv: (-kv[1], kv[0]))),
        "dimensions": {
            d: {k: dict(v) for k, v in values.items()} for d, values in dimensions.items()
        },
        "counties": [
            {"county": c, "name": display_name(county_names, c), "ballots": types}
            for c, types in sorted(counties.items())
        ],
    }


def build_analysis(
    *,
    mode,
    races,
    results_metadata,
    contest_rows,
    county_rows,
    history_points,
    history_rows,
    history_truncated,
    demographics_metadata,
    demographic_rows,
    county_demographic_rows,
    county_names,
    party_group,
):
    payload = {
        "state": "NC",
        "mode": mode,
        "methods": list(VOTE_METHODS),
        "results": None,
        "history": None,
        "demographics": summarize_demographics(
            demographics_metadata, demographic_rows, county_demographic_rows, county_names
        ),
        "notes": [
            "Unofficial returns. A lead is not a race call.",
            "Candidate votes and early/absentee ballot counts are separate official files; "
            "ballot counts are not votes for any candidate.",
        ],
    }
    if results_metadata is None:
        return payload
    contests, counties = defaultdict(list), defaultdict(list)
    for row in contest_rows:
        if row["contest_id"] not in races:
            raise ValueError("Published contest is outside the selected coverage")
        contests[row["contest_id"]].append(row)
    for row in county_rows:
        if row["contest_id"] not in races:
            raise ValueError("Published county contest is outside the selected coverage")
        counties[row["contest_id"]].append(row)
    if set(contests) != set(races):
        raise ValueError("Published snapshot lacks a selected contest")
    payload["results"] = {
        "publication": public_metadata(results_metadata),
        "races": [
            summarize_race(r, name, contests[r], counties[r], county_names, party_group)
            for r, name in races.items()
        ],
    }
    payload["history"] = {
        "status": results_metadata["status"],
        "truncated": history_truncated,
        "points": summarize_history(history_points, history_rows, races),
    }
    return payload


class NCAnalysis:
    """Read-only analysis of the approved NC results source and the demographics source."""

    def __init__(self, connect, source_id, races, county_names, party_group, demographics=None):
        self.connect, self.source_id, self.races = connect, source_id, races
        self.county_names, self.party_group = county_names, party_group
        self.demographics_source = demographics or DEMOGRAPHICS_SOURCE
        self.cache, self.lock = {}, Lock()

    @staticmethod
    def current(conn, source_id):
        return conn.execute(
            "SELECT p.* FROM serving.current_publications c "
            "JOIN serving.publications p USING (publication_id) WHERE c.source_id=%s",
            (source_id,),
        ).fetchone()

    @staticmethod
    def rows(conn, table, columns, publication_ids, extra=""):
        return conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE publication_id = ANY(%s) " + extra).format(
                sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier("nc", table)
            ),
            (publication_ids,),
        ).fetchall()

    def __call__(self):
        result_columns = ("contest_id", "choice", "choice_party", *VOTE_METHODS, "total_votes")
        with self.lock, self.connect() as conn:
            results = self.current(conn, self.source_id)
            demographics = self.current(conn, self.demographics_source)
            key = tuple(str(m["publication_id"]) if m else None for m in (results, demographics))
            if key in self.cache:
                return self.cache[key]
            contests, counties, points, history, truncated = [], [], [], [], False
            if results:
                identity = [str(results["publication_id"])]
                contests = self.rows(conn, "contest_candidate_results", result_columns, identity)
                counties = self.rows(
                    conn, "county_candidate_results", ("county", *result_columns), identity
                )
                points = conn.execute(
                    "SELECT * FROM serving.publications WHERE source_id=%s AND status=%s "
                    "AND published_at <= %s ORDER BY published_at DESC, publication_id DESC "
                    "LIMIT %s",
                    (self.source_id, results["status"], results["published_at"], HISTORY_LIMIT + 1),
                ).fetchall()
                truncated, points = len(points) > HISTORY_LIMIT, points[:HISTORY_LIMIT]
                history = self.rows(
                    conn,
                    "contest_candidate_results",
                    ("publication_id", *result_columns),
                    [str(p["publication_id"]) for p in points],
                )
            state_demo, county_demo = [], []
            if demographics:
                identity = [str(demographics["publication_id"])]
                state_demo = self.rows(
                    conn,
                    "state_absentee_demographics",
                    ("request_type", *DIMENSIONS, "ballot_count"),
                    identity,
                )
                county_demo = conn.execute(
                    "SELECT county, request_type, sum(ballot_count)::bigint AS ballot_count "
                    "FROM nc.county_absentee_demographics WHERE publication_id = ANY(%s) "
                    "GROUP BY county, request_type",
                    (identity,),
                ).fetchall()
            payload = build_analysis(
                mode="published",
                races=self.races,
                results_metadata=results,
                contest_rows=contests,
                county_rows=counties,
                history_points=points,
                history_rows=history,
                history_truncated=truncated,
                demographics_metadata=demographics,
                demographic_rows=state_demo,
                county_demographic_rows=county_demo,
                county_names=self.county_names,
                party_group=self.party_group,
            )
            self.cache = {key: payload}
            return payload


def demo_analysis(geography, races, party_group):
    """Explicitly synthetic data for local layout work; never a database fallback."""
    names = {f["id"]: f["name"] for f in geography["counties"]}
    house = [r for r in races if r != "NC-SEN"]
    candidates = (
        ("Example Democrat", "DEM"),
        ("Example Republican", "REP"),
        ("Example Libertarian", "LIB"),
    )
    final = defaultdict(lambda: dict.fromkeys(VOTE_METHODS, 0))
    counties = []
    for county in names:
        seed = int(hashlib.sha256(county.encode()).hexdigest()[:8], 16)
        size = 2000 + seed % 40000
        shares = (0.55, 0.42, 0.03) if seed % 3 else (0.38, 0.59, 0.03)
        selected = ["NC-SEN"] + ([house[seed % len(house)]] if house else [])
        for race in selected:
            if race not in races:
                continue
            for (choice, party), share in zip(candidates, shares):
                total = int(size * share)
                split = (total * 3 // 10, total * 6 // 10, total // 12)
                votes = dict(zip(VOTE_METHODS, (*split, total - sum(split))))
                counties.append(
                    {
                        "county": county,
                        "contest_id": race,
                        "choice": choice,
                        "choice_party": party,
                        **votes,
                        "total_votes": total,
                    }
                )
                for method, value in votes.items():
                    final[(race, choice, party)][method] += value
    contests = [
        {"contest_id": r, "choice": c, "choice_party": p, **v, "total_votes": sum(v.values())}
        for (r, c, p), v in final.items()
    ]
    meta = lambda pid, when: {  # noqa: E731
        "publication_id": pid,
        "source_id": "demo",
        "status": "test",
        "published_at": when,
        "source_modified_at": when,
        "first_observed_at": when,
        "source_url": None,
        "raw_sha256": None,
        "release_id": None,
    }
    points, history = [], []
    for step, fraction in enumerate((0.35, 0.7, 1.0)):
        when = f"2026-11-04T0{step + 1}:00:00+00:00"
        pid = f"demo-{step + 1}"
        points.append(meta(pid, when))
        for row in contests:
            scaled = {m: int(row[m] * fraction) for m in VOTE_METHODS}
            history.append(
                {**row, **scaled, "total_votes": sum(scaled.values()), "publication_id": pid}
            )
    demo_rows = []
    for kind, size in (
        ("EARLY VOTING", 9000),
        ("CIVILIAN", 900),
        ("MILITARY", 40),
        ("OVERSEAS", 120),
    ):
        for party, p in (("DEM", 0.39), ("REP", 0.27), ("UNA", 0.34)):
            for sex, s in (("FEMALE", 0.55), ("MALE", 0.45)):
                demo_rows.append(
                    {
                        "request_type": kind,
                        "party_registration": party,
                        "sex": sex,
                        "race": "UNDESIGNATED",
                        "ethnicity": "UNDESIGNATED",
                        "ballot_count": int(size * p * s),
                    }
                )
    return build_analysis(
        mode="demo",
        races=races,
        results_metadata=points[-1],
        contest_rows=contests,
        county_rows=counties,
        history_points=points,
        history_rows=history,
        history_truncated=False,
        demographics_metadata=meta("demo-ballots", "2026-11-04T03:00:00+00:00"),
        demographic_rows=demo_rows,
        county_demographic_rows=[],
        county_names=names,
        party_group=party_group,
    )
