# Adding a state

A state that stores statewide (and optionally county) candidate totals uses the shared
`total_only` dbt macros. Its dbt models, data tests, unit test, dbt variables and schema
migration are generated from its registry entry; only the parts that depend on the
official source are written by hand.

## Steps

1. **Register it** in `src/election_pipeline/states.py`: name, source ID, House seats,
   vote methods, and the flags that apply (`senate`, `governor`, `county_results`,
   `county_privacy_masking`, `collection_backend`, `method_note`). A schema that is a
   Python keyword (`in`, `or`) needs `parser_module`.
2. **Describe the source** in `config/sources.toml` under `sources.<source_id>`.
3. **Write the parser** `src/election_pipeline/parsers/<code>.py`, reusing a platform
   parser (`clarity`, `enhanced_voting`, `enr_platform`, `totals`) where the state uses one.
4. **Record the roster** in `src/election_pipeline/resources/rosters_2026/<CODE>.json`;
   `scripts/review_rosters.py` prints what a captured snapshot implies.
5. **Generate the rest:**

   ```powershell
   python scripts/new_state.py XX
   ```

   This writes `dbt/models/staging/xx/`, `dbt/models/marts/xx/`, `dbt/models/xx.yml`,
   `dbt/models/xx_unit_tests.yml`, `dbt/tests/xx_*.sql`, the next numbered
   `migrations/NNN_xx.sql`, and the state's entries in `dbt/dbt_project.yml` and
   `dbt/models/sources.yml`. Rerun it after changing the registry entry; it never
   rewrites a migration that already exists.
6. **Draw the map:** `python scripts/fetch_boundaries.py --states XX`.
7. **Add fixtures and tests** for what is specific to the state's source: captured
   official files with provenance, parser rejections, county reconciliation.

## What checks it

`tests/test_registry.py` runs over every registered state, so a new state is covered
without copying cohort tests. It fails when:

- a generated file differs from what the registry implies (`python scripts/new_state.py --check`
  lists them);
- a state lacks a source contract, parser, map or matching roster;
- a migration is not additive and scoped to its own schema, or its county table and
  method columns disagree with the registry.

## Exceptions

- NC, KY, MN, NE, TX and MS keep hand-written dbt models (`custom_models=True`).
- The CA and HI migrations were applied before the generator existed and are pinned by
  hash. Both allow county rows in `results` although neither stores any; a new
  statewide-only state gets the stricter `unit_type IN ('state')`.
