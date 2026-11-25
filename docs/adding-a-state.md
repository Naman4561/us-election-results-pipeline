# Adding a state

Every state shares the same tables, dbt models and data tests, parameterized by its
registry entry, so adding one usually needs no SQL. The exception is a vote method the
schema does not list yet: the allowed method names are a `CHECK` constraint, so a state
that reports its own needs a migration (Arizona's needed `028_arizona_methods.sql`).
What is written by hand is what depends on the official source: where it is, how to
read it, and who is on the ballot.

## Steps

1. **Register it** in `src/election_pipeline/states.py`: name, source ID, House seats
   (`(0,)` for at-large), vote methods, and the flags that apply (`senate`, `governor`,
   `county_results`, `county_privacy_masking`, `collection_backend`, `method_note`).
   A code that is a Python keyword (`in`, `or`) needs `parser_module`. A vote method not
   in `landing.result_methods`' check needs a migration that adds it.
2. **Describe the source** in `config/sources.toml` under `sources.<source_id>`: URL,
   format, expected bundle members, and `min_interval_seconds = 600` (every 15 minutes;
   see the note in that file). A manual source is described the same way; its URL is
   the page an operator saves files from, and nothing polls it.
3. **Write the parser** `src/election_pipeline/parsers/<code>.py`. Its `parse` reads the
   official format, reusing a platform reader where the state uses one (`clarity`,
   `enhanced_voting`, `enr_platform`, `tally`, `workbook`, `structured`, `html`), checks
   the roster (step 4) and hands the rows to `parsers.results.finish`, which checks
   races and methods and derives the totals the source does not print. Pass
   `reconcile=True` when the source prints statewide and county rows that must agree
   exactly.
4. **Record the roster** in `src/election_pipeline/resources/rosters_2026/<CODE>.json`:
   the candidates and counties of every race. The parser checks it with
   `parsers.scope.verify_rows` (or `verify_scope` per race; the platform readers already
   do). `scripts/review_rosters.py` prints what a captured snapshot implies; a person
   reviews it before it is committed.
5. **Draw the map:** `python scripts/fetch_boundaries.py --states XX` writes
   `dashboard/static/xx-map.json` from the pinned Census vintage (`tigerWMS_ACS2026`), the
   kind of place taken from the state's `local_unit`. Every response it downloads is kept
   by SHA-256 under `map-sources/` (committed), and `--offline` rebuilds the same file from that
   archive alone. To see what a rebuild would change before it replaces a map, write it
   elsewhere (`--output DIR`) and run `python scripts/compare_maps.py DIR`.
6. **Add fixtures and tests:** captured official files with a `provenance.json`
   (`tests/test_fixtures.py` checks every file's SHA-256 against it), an entry in
   `tests/snapshots.py` that stores them as the collector or `deploy import` would, and
   the figures they parse to in `PARSED` (`tests/test_every_source.py`). The source's own
   test file keeps what is specific to its format: its rejections (a missing county, a
   changed candidate, methods that do not add up) and the figures it reports. Run the dashboard's checks too
   (`npm test`, `tests/dashboard.test.cjs`): they count the map's states and pin how
   some states are shown, and CI runs them on every release.
7. **Document it:** its row in [states.md](states.md) (source, races, county detail,
   methods, known limits), its place in the platform diagram of
   [data collection](architecture/data-collection.md) (`tests/test_docs.py` checks
   every state is there once), and its count's status in
   [state-status.md](state-status.md).
8. **Deploy** it private first, then make it public:
   [adding a state in production](deployment.md#adding-a-state-in-production). A state
   whose files are saved by hand is configured for manual imports and imported as in
   [manual results](manual-results.md), which also covers how a multi-file import's zip
   is built.

## What checks it

`tests/test_registry.py` runs over every registered state, so a new one is covered
without copying tests. It fails when a state lacks a source, parser, map or a roster
matching its races (Alaska and Mississippi, which check a reviewed scope file of their
own, are named there instead), when its parser does not check the roster, when a
configured source belongs to no state, or when the races do not match the offices the
entry declares. `tests/test_every_source.py` runs every source's snapshot through the
same checks: it parses, verifies, converts and replays; it parses the same beside an
earlier snapshot with higher counts (a downward correction is just another snapshot);
it draws on its map and dashboard; and (with the test database) it loads, validates and
publishes, and the dashboard reads it back. It fails when a configured state has no
snapshot.

At run time the parser stops with `validation_failed` on an unexpected race, candidate
or county, or on methods that do not add up, and dbt's data tests stop publication on an
incomplete release, inconsistent candidate counts or county rows that do not reconcile
(`exact`, `masked` for privacy-masked states, or `none` without county results). Each
release checks every published source again under its new image, manual ones included.
