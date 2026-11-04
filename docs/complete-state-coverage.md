# Expand existing states to all House, Senate and governor races

| State | House | Senate | Governor | Total |
| --- | ---: | ---: | ---: | ---: |
| Texas | 38 | 1 | 1 | 40 |
| Minnesota | 8 | 1 | 1 | 10 |
| North Carolina | 14 | 1 | 0 | 15 |
| Nebraska | 3 | 1 | 1 | 5 |

This adds 54 races to these states' previous combined coverage of 16 races.
The map files already include every district. State legislative, judicial and
other statewide offices are outside this coverage contract.

## Design decisions

Each new source has an explicit `contests` list in `config/sources.toml` and a
separate ID ending in `_all`. The collector records that coverage in its signature,
and the parser and dbt build use the same contract. Empty `contests` retains the
original source's coverage and signature for replay and existing pinned images.

The four new sources use the existing state schemas, result tables, keys and
publication code. No database migration is required. A separately validated source
ID prevents an expanded publication from reaching an older dashboard that expects
fewer contests. Registration keeps it private; preparation downloads, parses,
reconciles, loads, tests and privately promotes each new snapshot. An interrupted
preparation leaves the existing public sources running. Before registration,
preparation waits for any already-started expansion, backup and monitor jobs to
finish, so an older controller never receives catalog fields it cannot parse.

Activation uploads a backup, restores it into an isolated database, verifies the
exact four publication IDs and permissions, chooses a free localhost port, checks
the candidate API, and switches Caddy and the catalog with the existing handoff
journal. The dashboard obtains source IDs and races from its coverage catalog.
County detail, unknown counts, manual Ohio labels, and client-side navigation use
their existing behavior.

Old Texas polling is disabled during activation. Baseline collectors keep running
until a separate retirement command updates their future source selection during
an idle interval. Their images and original data remain available for rollback.
The previous dashboard and its source flags are retained. After retirement, a
rollback runs the original source images under the independent worker.

## Official data and validation limits

- NC: the existing official precinct ZIP already contains Senate and all 14 House
  districts. Voting-method arithmetic, duplicate detection and reconciliation remain enabled.
- MN: retain the five election-dated media files, add `Governor.txt` and
  `governorpct.txt`, aggregate governor precincts to counties, require complete
  candidate/precinct coverage in all 87 counties, and reconcile official totals.
  House office IDs run from `0104` through `0111`.
- NE: the official media page's `type=All` CSV includes congressional races.
  Require `AreaType=CG` and `AreaNum=District 01` through `District 03` for House;
  require statewide grain for Senate and governor. County detail remains unavailable.
- TX: select every House district from the existing Civix Federal section, plus
  Senate and governor. Each selected candidate sum must equal the feed's reported
  contest total. An explicit expanded contract permits an uncontested candidate list.
  It does not infer missing candidates or invent zero votes.

Official NC, MN and NE downloads were parsed during development. Texas returned
HTTP 403 from the development network, so its full feed must pass preparation on
Lightsail. Offline Texas additions are synthetic tests, not an original full-feed
capture. All states still need production preparation and restore verification.
See `coverage-validation-2026-11-04.json` for the captured-file evidence.

## Validate locally before committing

Run from Windows PowerShell at the repository root:

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_coverage.py
npm test
```

The PostgreSQL command creates and removes a disposable test database. The new
database test builds and publishes all four expanded sources. Linux host-lock
tests are skipped on Windows and run in the staged image on Ubuntu.

## Roll out on Ubuntu

Push the tested branch `feat/complete-state-coverage` first. Paste one command at
a time; none of the operator commands needs a backslash continuation.

```bash
coverageStage="$(mktemp -d /tmp/election-coverage.XXXXXXXX)"
git clone --depth 1 --branch feat/complete-state-coverage https://github.com/Naman4561/us-election-results-pipeline.git "$coverageStage"
sudo bash "$coverageStage/scripts/stage_coverage_release.sh"
coverageRelease="$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env)"
deployElection coverage prepare --states TX MN NC NE --release "$coverageRelease" --image "election-pipeline:${coverageRelease##*/}"
deployElection coverage activate --states TX MN NC NE
deployElection coverage retire --states TX MN NC NE
deployElection status
sudo systemctl start election-monitor.service
sudo cat /var/lib/election-pipeline-monitor/latest-check.json
```

Stop at an error. `prepare` must report `prepared` before `activate`, which must
report `activated` before `retire`. If retirement finds a running baseline cycle,
repeat only the retirement command after that cycle finishes. Each phase can use
a subset of the four states when a source is blocked. Production fetch, dbt, API
and restore gates still apply to that subset.

The `_all` sources should be public and enabled after activation. Original source
IDs should be private and disabled after retirement. Timers, database containers
and the public proxy continue running throughout the rollout.

For dashboard rollback, use `deployElection rollback-dashboard`. If a host crash
interrupts retirement and leaves a pending `source-handoff.json`, use
`deployElection recover-coverage-retirement` during an idle baseline interval,
then retry retirement. Recovery preserves the expanded public dashboard.
