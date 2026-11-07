"""Generate a state's dbt models, data tests and schema migration from its registry entry.

A state using the shared ``total_only`` dbt macros needs no hand-written SQL or YAML:
everything here follows from its ``State`` in ``election_pipeline.states`` (races, vote
methods, county results, privacy masking) and the shared dbt project. Register the state
there first, then run

    python scripts/new_state.py XX           # write the state's files
    python scripts/new_state.py --check      # report any state whose files have drifted

An existing state's migration is never rewritten: applied migrations are pinned by their
sha256 in ``ops.schema_migrations``. A new state gets the next free number. Still by
hand: the parser, the source contract in ``config/sources.toml``, the reviewed roster in
``resources/rosters_2026``, the map (``scripts/fetch_boundaries.py``) and fixtures.
"""

import argparse
import sys
from pathlib import Path

import yaml

from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
DBT = ROOT / "dbt"
MIGRATIONS = ROOT / "migrations"

# Two-letter PostgreSQL reserved words that are also state codes (Indiana, Oregon).
RESERVED = {"in", "or"}

# Applied before the generator existed and pinned by hash. Each permits county rows in
# its results table although the state stores none; new statewide-only states do not.
FROZEN_MIGRATIONS = {"CA", "HI"}


def generated(state):
    return not state.custom_models


def methods(state):
    """Vote-method columns besides total_votes; each adds up to the total."""
    return list(state.methods[1:])


def dump(value):
    return yaml.safe_dump(value, sort_keys=False)


def not_null(*columns):
    return [{"name": column, "data_tests": ["not_null"]} for column in columns]


def unique(*columns):
    return {"unique_combination": {"arguments": {"columns": list(columns)}}}


def schema_yml(state):
    s = state.schema
    key = ("release_id", "unit_type", "county", "contest_id", "source_candidate_id")
    models = [
        {"name": f"stg_{s}_releases", "data_tests": [unique("release_id")]},
        {"name": f"stg_{s}_results", "data_tests": [unique(*key)], "columns": not_null(*key)},
        {
            "name": f"{s}_contest_candidate_results",
            "data_tests": [
                unique("release_id", "contest_id", "choice"),
                "vote_shares_match_counts",
            ],
            "columns": not_null("release_id", "contest_id", "choice"),
        },
    ]
    models[0]["columns"] = not_null("release_id")
    if state.county_results:
        county = ("release_id", "county", "contest_id", "choice")
        models.append(
            {
                "name": f"{s}_county_candidate_results",
                "data_tests": [unique(*county)],
                "columns": not_null(*county),
            }
        )
    return dump({"version": 2, "models": models})


def unit_tests_yml(state, election_date):
    """Shares are computed per release and race from statewide rows only."""
    s, races = state.schema, list(state.races)
    first, second = races[0], races[1] if len(races) > 1 else f"{state.code}-OTHER"

    def row(release, unit, county, contest, choice, votes):
        values = {
            "release_id": release,
            "source_id": state.source_id,
            "election_date": election_date,
            "state": state.code,
            "unit_type": unit,
            "county": county,
            "contest_id": contest,
            "choice": choice,
            "choice_party": "REP",
            "total_votes": votes,
        }
        for n, method in enumerate(methods(state)):
            values[method] = votes if n == 0 else 0
        return values

    rows = [
        row(1, "state", "", first, "A", 3),
        row(1, "state", "", first, "B", 7),
        row(1, "state", "", second, "C", 20),
        row(1, "county", "COUNTY", first, "A", 99),
        row(2, "state", "", first, "A", 0),
    ]
    expect = [
        {"release_id": 1, "choice": "A", "contest_votes": 10, "vote_share": "0." + "3" + "0" * 19},
        {"release_id": 1, "choice": "B", "contest_votes": 10, "vote_share": "0." + "7" + "0" * 19},
        {"release_id": 1, "choice": "C", "contest_votes": 20, "vote_share": "1." + "0" * 20},
        {"release_id": 2, "choice": "A", "contest_votes": 0, "vote_share": None},
    ]
    test = {
        "name": f"{s}_shares_separate_releases_races_and_counties",
        "model": f"{s}_contest_candidate_results",
        "given": [{"input": f"ref('stg_{s}_results')", "rows": rows}],
        "expect": {"rows": expect},
    }
    return dump({"version": 2, "unit_tests": [test]})


def data_test(state, macro):
    return f"{{{{ config(tags=['state_{state.schema}']) }}}}\n{{{{ {macro}('{state.schema}') }}}}\n"


def migration(state):
    s = state.schema
    q = f'"{s}"' if s in RESERVED else s
    cols = methods(state)
    columns = "".join(f"    {m} bigint NOT NULL CHECK ({m} >= 0),\n" for m in cols)
    adds = f"    CHECK ({' + '.join(cols)} = total_votes)" if cols else ""
    units = "'state','county'" if state.county_results else "'state'"
    out = [
        f"-- Additive schema for {state.name}. Existing state tables are untouched.\n",
        "BEGIN;\n",
        f"CREATE SCHEMA {q};\n",
        f"REVOKE ALL ON SCHEMA {q} FROM PUBLIC;\n",
        f"CREATE TABLE {q}.results (\n",
        "    release_id bigint NOT NULL REFERENCES ops.processing_releases,\n",
        "    source_line integer NOT NULL CHECK (source_line > 1),\n",
        f"    unit_type text NOT NULL CHECK (unit_type IN ({units})),\n",
        "    county text NOT NULL,\n",
        "    contest_id text NOT NULL,\n",
        "    source_contest_id text NOT NULL,\n",
        "    source_candidate_id text NOT NULL,\n",
        "    choice text NOT NULL CHECK (btrim(choice) <> ''),\n",
        "    choice_party text,\n",
        "    total_votes bigint NOT NULL CHECK (total_votes >= 0),\n",
        columns,
        "    precincts_reporting bigint CHECK (precincts_reporting >= 0),\n",
        "    precincts_total bigint CHECK (precincts_total >= 0),\n",
        "    PRIMARY KEY (release_id, source_line),\n",
        "    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),\n",
        "    UNIQUE (release_id, unit_type, county, contest_id, choice),\n",
        "    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),\n",
        "    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),\n",
        "    CHECK (precincts_reporting <= precincts_total)"
        + (",\n" + adds if adds else "")
        + "\n",
        ");\n",
    ]
    if state.county_results:
        out += [
            f"CREATE TABLE {q}.county_candidate_results (\n",
            "    publication_id uuid NOT NULL REFERENCES serving.publications,\n",
            "    county text NOT NULL,\n",
            "    contest_id text NOT NULL,\n",
            "    choice text NOT NULL,\n",
            "    choice_party text,\n",
            "    total_votes bigint NOT NULL CHECK (total_votes >= 0),\n",
            columns,
            "    PRIMARY KEY (publication_id, county, contest_id, choice)"
            + (",\n" + adds if adds else "")
            + "\n",
            ");\n",
        ]
    out += [
        f"CREATE TABLE {q}.contest_candidate_results (\n",
        "    publication_id uuid NOT NULL REFERENCES serving.publications,\n",
        "    contest_id text NOT NULL,\n",
        "    choice text NOT NULL,\n",
        "    choice_party text,\n",
        "    total_votes bigint NOT NULL CHECK (total_votes >= 0),\n",
        columns,
        "    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),\n",
        "    vote_share numeric,\n",
        "    PRIMARY KEY (publication_id, contest_id, choice),\n",
        "    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),\n",
        "    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),\n",
        "    CHECK (total_votes <= contest_votes),\n",
        adds + ",\n" if adds else "",
        "    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR\n",
        "           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR\n",
        "           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND\n",
        "            vote_share=total_votes::numeric / contest_votes))\n",
        ");\n",
        f"GRANT USAGE ON SCHEMA {q} TO election_dashboard;\n",
    ]
    tables = [f"{q}.contest_candidate_results"]
    if state.county_results:
        tables.append(f"{q}.county_candidate_results")
    out += [f"GRANT SELECT ON {', '.join(tables)} TO election_dashboard;\n", "COMMIT;\n"]
    return "".join(out)


def migration_path(state):
    existing = sorted(MIGRATIONS.glob(f"[0-9][0-9][0-9]_{state.schema}.sql"))
    if existing:
        return existing[0]
    number = max(int(p.name[:3]) for p in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")) + 1
    return MIGRATIONS / f"{number:03}_{state.schema}.sql"


def files(state, election_date):
    """Every per-state file, keyed by its path relative to the repository root."""
    s = state.schema
    out = {
        f"dbt/models/staging/{s}/stg_{s}_releases.sql": f"{{{{ total_only_releases('{s}') }}}}\n",
        f"dbt/models/staging/{s}/stg_{s}_results.sql": f"{{{{ total_only_results('{s}') }}}}\n",
        f"dbt/models/marts/{s}/{s}_contest_candidate_results.sql": (
            f"{{{{ total_only_contests('{s}') }}}}\n"
        ),
        f"dbt/models/{s}.yml": schema_yml(state),
        f"dbt/models/{s}_unit_tests.yml": unit_tests_yml(state, election_date),
        f"dbt/tests/{s}_candidate_counts.sql": data_test(state, "total_only_candidate_counts"),
        f"dbt/tests/{s}_release_completeness.sql": data_test(
            state, "total_only_release_completeness"
        ),
    }
    if state.county_results:
        out[f"dbt/models/marts/{s}/{s}_county_candidate_results.sql"] = (
            f"{{{{ total_only_counties('{s}') }}}}\n"
        )
        macro = (
            "total_only_masked_county_reconciliation"
            if state.county_privacy_masking
            else "total_only_county_reconciliation"
        )
        out[f"dbt/tests/{s}_county_reconciliation.sql"] = data_test(state, macro)
    if methods(state):
        out[f"dbt/tests/{s}_vote_methods.sql"] = data_test(state, "total_only_vote_methods")
    if state.code not in FROZEN_MIGRATIONS:
        out[migration_path(state).relative_to(ROOT).as_posix()] = migration(state)
    return out


def project(value, state):
    """dbt_project.yml with this state's source, races, methods and tags in place."""
    s, variables = state.schema, value["vars"]
    variables[f"{s}_results_source"] = state.source_id
    variables[f"{s}_races"] = list(state.races)
    if methods(state):
        variables[f"{s}_methods"] = methods(state)
    else:
        variables.pop(f"{s}_methods", None)
    for layer in ("staging", "marts"):
        value["models"]["election_analytics"][layer][s] = {"+tags": [f"state_{s}"]}
    return value


def sources(value, state):
    """sources.yml with this state's landing table declared."""
    entry = {"name": state.schema, "schema": state.schema, "tables": [{"name": "results"}]}
    for n, existing in enumerate(value["sources"]):
        if existing["name"] == state.schema:
            value["sources"][n] = entry
            return value
    value["sources"].append(entry)
    return value


def read(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None


def plan(codes):
    """Desired contents of every generated file, including the shared dbt YAML."""
    project_path, sources_path = DBT / "dbt_project.yml", DBT / "models/sources.yml"
    project_value = yaml.safe_load(read(project_path))
    sources_value = yaml.safe_load(read(sources_path))
    election_date = project_value["vars"]["election_date"]
    out = {}
    for code in codes:
        state = STATES[code]
        out.update(files(state, election_date))
        project(project_value, state)
        sources(sources_value, state)
    out["dbt/dbt_project.yml"] = dump(project_value)
    out["dbt/models/sources.yml"] = dump(sources_value)
    return out


def drift(codes):
    return sorted(path for path, text in plan(codes).items() if read(ROOT / path) != text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("states", nargs="*", help="Two-letter codes registered in states.py")
    parser.add_argument("--check", action="store_true", help="Report drift; write nothing")
    args = parser.parse_args()

    codes = [code.upper() for code in args.states]
    for code in codes:
        if code not in STATES:
            parser.error(f"{code} is not registered in election_pipeline/states.py")
        if not generated(STATES[code]):
            parser.error(f"{code} keeps hand-written dbt models")
    if args.check:
        codes = codes or [code for code, state in STATES.items() if generated(state)]
        stale = drift(codes)
        for path in stale:
            print(f"out of date: {path}")
        return 1 if stale else 0
    if not codes:
        parser.error("Name at least one state, or pass --check")
    for path, text in plan(codes).items():
        target = ROOT / path
        if read(target) == text:
            continue
        if path.startswith("migrations/") and target.exists():
            sys.exit(f"Refusing to rewrite applied migration {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
