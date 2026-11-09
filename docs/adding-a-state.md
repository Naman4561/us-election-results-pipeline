# Adding a state

Every state shares the same tables, dbt models and data tests, parameterized by its
registry entry, so adding one needs no migration or SQL. What is written by hand is what
depends on the official source: where it is, how to read it, and who is on the ballot.

## Steps

1. **Register it** in `src/election_pipeline/states.py`: name, source ID, House seats
   (`(0,)` for at-large), vote methods, and the flags that apply (`senate`, `governor`,
   `county_results`, `county_privacy_masking`, `collection_backend`, `method_note`).
   A code that is a Python keyword (`in`, `or`) needs `parser_module`.
2. **Describe the source** in `config/sources.toml` under `sources.<source_id>`: URL,
   format, expected bundle members, and `min_interval_seconds = 840` (every 15 minutes).
3. **Write the parser** `src/election_pipeline/parsers/<code>.py`. Its `parse` reads the
   official format, reusing a platform reader where the state uses one (`clarity`,
   `enhanced_voting`, `enr_platform`, `workbook`, `structured`, `html`), and hands the rows
   to `parsers.results.finish`, which checks races, candidates and methods and derives
   the totals the source does not print. Pass `reconcile=True` when the source prints
   statewide and county rows that must agree exactly.
4. **Record the roster** in `src/election_pipeline/resources/rosters_2026/<CODE>.json`:
   the candidates of every race. `scripts/review_rosters.py` prints what a captured
   snapshot implies; a person reviews it before it is committed.
5. **Draw the map:** `python scripts/fetch_boundaries.py --states XX` writes
   `dashboard/static/xx-map.json`.
6. **Add fixtures and tests** for what is specific to the source: captured official
   files with provenance, its rejections (a missing county, a changed candidate, methods
   that do not add up) and its county reconciliation.
7. **Deploy** it private first, then make it public:
   [adding a state in production](deployment.md#adding-a-state-in-production).

## What checks it

`tests/test_registry.py` runs over every registered state, so a new one is covered
without copying tests. It fails when a state lacks a source, parser, map or matching
roster, when a configured source belongs to no state, or when the races do not match the
offices the entry declares.

At run time the parser stops with `validation_failed` on an unexpected race, candidate
or county, or on methods that do not add up, and dbt's data tests stop publication on an
incomplete release, inconsistent candidate counts or county rows that do not reconcile
(`exact`, `masked` for privacy-masked states, or `none` without county results).
