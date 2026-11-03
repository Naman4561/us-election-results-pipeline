# Multi-state collection and publication

Decision date: 2026-11-03. Baseline: f4a67d001e9fd4d916911b9db7efb44d62cf5d28.

This expansion adds Kentucky Senate and all six House districts. North Carolina
retains Senate and NC-01, NC-03, NC-04 and NC-11. Neither state has a governor election
in 2026. The governor tab records ballot status nationally and is ready for later coverage.

## Storage and ownership

`ops` holds shared source, snapshot, load and publication audit records. Physical state
result and demographic tables live in `nc` and `ky`. Shared `serving` holds publication
metadata and one current pointer per source. Old NC relation names are compatibility
views; existing publications and IDs survive migration 003. Never edit 001 or 002.

The dashboard role can select published state tables and shared publication metadata.
It cannot select `nc.results`, `ky.results` or other ingestion tables. This allows
one read-only application while preserving state-specific table definitions.

A state adapter declares coverage, available vote methods, its parser and loading
contract. Shared collection, parsing orchestration, loading, publication and rendering
consume those contracts. State-specific parsing belongs in `parsers/<state>.py`;
state-specific SQL belongs in `dbt/models/{staging,marts}/<state>`.

## Independent stages and concurrency

Collect, parse and load keep their existing CLI commands. The publication CLI adds:

```
python scripts/publish.py transform --release-id <id> --status test
python scripts/publish.py promote --candidate-id <uuid>
```

The first command builds **only that state's** dbt graph in invocation-specific schemas,
runs every applicable model/data/unit test and fingerprints the resulting tables.
The second verifies the release, transformation hash and fingerprints before atomically
copying frozen results and changing the source pointer. `publish` remains a convenience
command for both. `restore` reactivates a previously validated publication.

State publication/restore uses one exclusive advisory lock per state. Publishers also
hold a shared global maintenance lock; backups hold that lock exclusively. Thus NC
and KY can overlap while same-state publication and maintenance remain serialized.
A failed state cannot roll back another state's completed publication.

The production runner uses a bounded thread pool (`PIPELINE_WORKERS=2`, range 1–8).
Each dbt run has separate schemas, target files, logs and child-process environment.
The database and dashboard remain shared processes. More workers consume more RAM;
start with two and measure on the Lightsail instance before increasing it.

Prepared candidates are not publications. Their temporary schemas are omitted from
backups. A restored pending candidate must be transformed again. Validated candidates
that were never promoted may be removed after reviewing their attempt records; never
remove schemas belonging to an active run. Raw snapshots and published tables are
included in backup inventory and checksum verification.

## Kentucky's source contract

Official hub: https://vrsws.sos.ky.gov/liveresults/Data
Guide: https://vrsws.sos.ky.gov/liveresults/Documentation/DataWebsiteGuidev6.pdf

Collect `CurrentResultsExcludeLocal` and five reference documents as one deterministic
ZIP. The ZIP is our replay container; the state serves XML, not this ZIP. Component
URLs, response metadata and SHA-256 values are retained with each fetch attempt.
References are downloaded before results. The endpoints do not offer an atomic snapshot
across documents; missing IDs, scope errors or reconciliation failures block publication
and a later poll retries the bundle. Five minutes is our cadence, not a guaranteed
vendor rate limit. Respect Retry-After and existing backoff limits.

Preserve state, county and precinct levels in `ky.results`. **Do not sum all levels**.
County rows feed county marts; their sums must equal the separately supplied statewide
candidate totals. Reporting-unit statuses are preserved in `ky.reporting_units`.
`PrecinctsReporting` does not mean the precinct is complete or establish a percentage
of votes counted. Counts can decrease when officials correct results.

Only total, Election Day and combined absentee votes are available. Combined absentee
is derived as total minus Election Day; early/mail/provisional are unavailable, not zero.
No historical demographics or Kentucky demographics are added by this change.

A fresh pre-reporting download parsed 26,647 federal rows, 3,310 reporting-unit records,
120 Senate counties and all seven federal contests. All votes were zero. This establishes
structural agreement only; independent live-result verification remains an Election Day gate.

## Dashboard contract

The national default has Senate, House and Governor tabs. Senate/Governor use state
shapes. House uses 435 district shapes. Covered states open their state view. State
Senate/Governor use counties; state House uses districts. Changing office preserves
national/state scope, including an explicit no-election message when appropriate.

Map statuses are separate: no election, election outside coverage, covered awaiting
votes, Democratic lead, Republican lead, other lead, tie/unknown party and unavailable.
An unavailable API is never interpreted as a zero-vote report. Test publications and
synthetic demo data have persistent labels. Leads are not race calls. National counters
include only covered races and do not assert chamber control.

`/api/snapshot` preserves the NC contract for existing monitoring. New consumers use
`/api/national` or `/api/states/NC`, `/api/states/KY`. Each state response pins its metadata
and rows in a read-only repeatable-read transaction and caches by publication ID.
National responses identify independent state publications; they are not a nationwide
atomic reporting cutoff. Tabs and county selections use the already fetched snapshot.
API routes remain uncached at the CDN; static resources use versioned URLs.
Verify the CDN bypass covers `/api/*`, including `/api/national` and `/api/states/*`;
an exception for only `/api/snapshot` does not cover the new routes.

Geometry is generated from Census TIGERweb. The national House map uses 120th Congress
districts with Missouri explicitly reverted to the 119th plan. Sources and geometry
hashes are embedded in each artifact. Hawaii’s national inset shows the main islands. Run `python scripts/fetch_boundaries.py` to refresh
NC/KY and national geometry. Review legal boundary changes before replacing the map.

## Adding another state

1. Research and document the official endpoint, cadence, field meanings and references.
2. Add its TOML source and `State` entry (including Senate/Governor coverage flags).
3. Implement `parse`, `load_contract`, and `database_row` in its parser module.
4. Add an additive migration for its ingestion and published tables; grant the reader
   only published-table SELECT. Add dbt models tagged `state_<code>`, unit tests and
   a completeness test whose name ends in `release_completeness`.
5. Generate its county/district map with `--states <code>` and check geography IDs.
6. Pass known-answer parser, reconciliation, idempotency, correction, publication,
   independent agreement, permission and recovery checks before adding its source
   to production `PIPELINE_SOURCES`. No additional frontend state switches are needed.

## Local validation and rollout gate

Work on this branch locally while the deployed NC rehearsal stays on its current commit.
Install the postgres, transform and dashboard extras. Run Python tests, lint, dbt parse,
DOM navigation checks and the disposable native PostgreSQL acceptance suite:

```
python -m pip install -e ".[postgres,transform,dashboard]" -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
npm ci
npm test
python scripts/check_pipeline.py
python -m election_pipeline.dashboard.server --demo
```

`check_pipeline.py` creates and removes its own `_test` database; it does not migrate
production. The native suite includes actual state-lock concurrency, simultaneous dbt
publication, rollback, correction, replay and reader permission checks. Offline tests
that skip native checks do not satisfy this gate.

After rehearsal and native acceptance pass: create a verified backup; pause pipeline and
backup timers; deploy one reviewed commit; apply 003 through the existing checksum-tracked
production bootstrap; build/restart the dashboard and workers together. Do not run old
COPY-based NC loading code after the migration. Test both states, take and restore a
new backup, point monitoring to `/api/national`, and then set:

```
PIPELINE_SOURCES=nc_2026_general,ky_2026_general
PIPELINE_WORKERS=2
PIPELINE_STATUS=test
```

Resume scheduling after successful manual runs. Check memory, execution time, state-specific
alerts, CDN API responses and browser navigation. Keep `test` until the explicit live-data
transition. Rolling back code alone does not reverse a schema migration; recover with the
verified backup or a reviewed forward fix.
