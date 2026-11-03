# Minnesota, Nebraska and Texas

Implementation baseline: `30f69aed32776cd162d75eb0f24949d6e4cd786e`.
NC/KY production acceptance is complete by the operator's confirmation. This batch
adds 11 watched races, for 23 across five states after activation.

| State | Watched races | Available detail |
| --- | --- | --- |
| Minnesota | Senate, MN-01 | County and official contest totals |
| Nebraska | Senate, Governor | Official contest totals |
| Texas | Senate, Governor, TX-15, TX-23, TX-28, TX-34, TX-35 | Official contest totals |

The House selections match the user's November 2 v4 forecast's competitive list for
these states. Senate/governor selections retain the saved rollout watchlist. No new
competitiveness forecast or race call is made here. Other House races, Minnesota's
governor race and new demographic sources are outside this batch.

## Source evidence and limitations

Research and actual collector/parser checks were performed November 3, 2026.
Each configured component returned HTTP 200 from the assistant workspace. This
does not establish access from Lightsail: perform the production-host shadow runs
below before enabling scheduling.

- Minnesota official index:
  https://electionresults.sos.mn.gov/Select/MediaFiles/Index?ersElectionId=201
  and field specification:
  https://electionresults.sos.mn.gov/Results/MediaFileLayout/Index?erselectionId=201 .
  Semicolon exports have 16 fields. The county table contains 87 county IDs, which
  are source IDs, not assumed Census FIPS codes. The feed supplies Senate totals,
  Senate by county, House totals and House by county. Downloads are updated every
  15 minutes or less according to the index. Precinct-level results are not
  available until the next morning according to the specification.
- Nebraska official media index:
  https://electionresults.nebraska.gov/ResultsMedia.aspx .
  Its statewide CSV route is configured verbatim in `sources.toml`. Data rows have
  one extra empty trailing field; only that exact shape is normalized. The
  precinct field is a literal `="reported/total"` expression, parsed as text.
  No spreadsheet formulas are executed. County exports were not established as
  complete, so this adapter publishes race totals only.
- Texas official statewide page:
  https://electionresults.sos.state.tx.us/results.html .
  Selected server-rendered tables contain Party, Candidate and Ballots Cast.
  The page identifies the November 3, 2026 general election. No documented bulk
  county feed or vote-method split has been established for this adapter. It does
  not execute JavaScript or issue a request per county/contest.

The captured Minnesota and Nebraska counts were all zero. Texas vote cells were
blank. Actual collector output parsed 506 MN rows (87 Senate counties and 21
MN-01 counties), nine NE rows, and 28 TX rows. The fixture directory records source
provenance. Positive-vote fixtures are synthetic and do not establish live accuracy.

Each source's five-minute minimum is our configuration, not a published rate-limit
guarantee. This batch makes nine requests per full poll: MN six, NE two, TX one.
Respect backoff/Retry-After. A 403 remains terminal for that collection run and
requires investigating the restriction; do not rotate identities or bypass it.
References and result files are not an atomic server-side transaction. Minnesota
may temporarily fail reconciliation when downloads straddle a refresh. Retain the
last valid publication and retry at the next scheduled check.

## Design decisions

- Each adapter owns its mapping and state-specific tests. The shared `totals.py`
  contract handles storage and common validations for total-only feeds. The bundle
  collector now accepts a configured primary filename, preserving original bytes,
  per-component URL/hash metadata and deterministic replay containers. NC/KY source
  signatures retain their existing default filename representation.
- Migration 004 adds `mn`, `ne`, `tx` schemas without changing migrations 001–003.
  Source/candidate IDs, geographic grain, votes and available precinct counts live
  in each state's `results` table. `source_line` numbers the normalized snapshot;
  source IDs and the immutable bundle identify original records. TX candidate
  identity is the published label because this page does not supply candidate IDs.
- Only MN has a published county table. Publication targets and dashboard queries
  follow `State.county_results`; they never require fake county rows for NE/TX.
  Reader grants cover frozen published tables, not ingestion tables.
- TX blank votes become CSV empty fields, SQL NULL and JSON null. An all-blank
  contest has unknown total/share and a dash in the dashboard. Numeric zero remains
  a known zero. A contest with mixed blank/numeric candidate counts is held for
  review rather than producing a partial denominator. Blank counts accompanied by
  nonzero polling-location reporting also stop parsing until their meaning is
  reviewed. Polling-location percentages are not percentages of votes counted.
- Minnesota checks each candidate's 87-county Senate coverage, all county/contest
  vote sums, the repeated official office denominator, and summed precinct
  denominators. Missing zero-vote counties therefore cannot pass just because the
  statewide count is zero. DFL maps to Democratic blue.
- State maps use counties when supported and a state outline otherwise. The House
  view retains district geometry and aggregate district totals. Office tabs preserve
  scope and explicitly distinguish elections outside coverage from no election.
- `run --prepare-only` collects, parses, loads and runs the complete state dbt build
  without moving the public pointer. It returns a candidate ID for explicit promotion.
  Existing scheduled runs retain their behavior. Prepared schemas are temporary and
  are still excluded from backups; frozen publications and all three new schemas
  are included by the existing registry-driven backup/restore inventory.

## Validation recorded in the assistant workspace

Official source collection and parsing, Python offline tests, lint/format checks,
dbt parse, and DOM interaction checks pass. PostgreSQL/WASM checks exercised all
four migrations, the five states' SQL models, completeness/reconciliation checks,
frozen publication inserts, NULL preservation and published-only reader grants.
They also exercised nonzero shares and rejected a county mismatch.

Native PostgreSQL is unavailable in this workspace. Run `scripts/check_pipeline.py`
on the existing laptop database or use the repository's PostgreSQL 17 CI job.
The new native test loads and independently prepares/promotes all three sources,
asserts that preparation leaves the public pointer absent, checks replay and reader
permissions. Existing NC/KY publication/concurrency/recovery checks remain in the suite.

## Local installation and checks

Apply the supplied incremental patch from the repository root. It is based on the
deployed NC/KY commit above. A `git apply --check` failure needs review; do not force
an application over different edits.

PowerShell:

```powershell
git switch -c feature/mn-ne-tx
git apply --check --whitespace=nowarn "$env:USERPROFILE\Downloads\mn-ne-tx-expansion.patch"
git apply --whitespace=nowarn "$env:USERPROFILE\Downloads\mn-ne-tx-expansion.patch"
.\.venv\Scripts\python -m pip install -e ".[postgres,transform,dashboard]" -r requirements-dev.txt
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m unittest discover -s tests -q
npm ci
npm test
.\.venv\Scripts\python scripts/check_pipeline.py
```

Inspect the demo with `python -m election_pipeline.dashboard.server --demo`.
After the checks pass, commit and push the branch:

```powershell
git add .gitattributes config dbt docs migrations scripts src tests
git commit -m "feat: add Minnesota Nebraska and Texas result adapters"
git push -u origin feature/mn-ne-tx
```

Use a native test pass and the checked Git revision as the deployment gate. Do not
enable new sources just because offline tests pass.

## Production: shadow first, activate each passing source

The following commands are for Ubuntu in `/opt/election-pipeline`, using the existing
`dc` function and private environment files. Do not paste production secrets into chat.
Before switching code, take the existing successful uploaded backup. Pause scheduling
briefly and let active workers/backups finish before changing the checkout. Use the
existing backup/monitor maintenance procedure so the pause is intentional.

```bash
cd /opt/election-pipeline
sudo systemctl start election-backup.service
sudo systemctl show election-backup.service -p Result -p ExecMainStatus
sudo systemctl stop election-pipeline.timer election-backup.timer
sudo systemctl is-active election-pipeline.service election-backup.service
```

Wait for both services to become inactive. Check that the uploaded backup succeeded
and save its ID. Fetch the reviewed revision, check the working tree is clean, then:

```bash
git fetch origin feature/mn-ne-tx
git status --short
git switch feature/mn-ne-tx
git pull --ff-only
export IMAGE_TAG="$(git rev-parse HEAD)"
dc --profile tools build pipeline
dc --profile tools run --rm bootstrap
sudo .venv/bin/python -m pip install -e '.[postgres,transform,dashboard]' -r requirements-dev.txt
```

Bootstrap applies 004 through the checksum ledger. Persist the new `IMAGE_TAG` in
`/etc/election-pipeline/production.env` using `sudoedit`; keep existing enabled source
IDs and worker count. No image is selected by merely exporting a shell variable if
your `dc` wrapper explicitly overrides it: ensure the private env and shell agree.

Keep the old dashboard running until all intended first publications pass their gates.
The old dashboard only queries existing state tables, which migration 004 preserves.
Complete the following three one-time shadow runs sequentially during the maintenance
window, keeping the total running workload bounded. If investigation is needed, resume
NC/KY timers on the reviewed image and revisit the failed state separately.

```bash
dc --profile tools run --rm pipeline python -m election_pipeline run --source mn_2026_general --status test --prepare-only
dc --profile tools run --rm pipeline python -m election_pipeline run --source ne_2026_general --status test --prepare-only
dc --profile tools run --rm pipeline python -m election_pipeline run --source tx_2026_general --status test --prepare-only
```

Run these once each. Success is `outcome: validated` with a `candidate_id`, not a
public result. A failure in one source does not authorize promoting it or block a
different source that passed. Record the IDs and compare the candidate identities
and available counts with the official state pages. Zero/blank agreement is only
pre-reporting validation. Do not repeatedly create unused prepared schemas.

For each candidate that passed its applicable check, replace `CANDIDATE_UUID`:

```bash
dc --profile tools run --rm pipeline python scripts/publish.py promote --candidate-id CANDIDATE_UUID
```

Then restart the dashboard on the new image:

```bash
dc up -d dashboard
curl -fsS http://127.0.0.1:8000/api/national
```

Only after the selected first publications are valid, add those source IDs in
`/etc/election-pipeline/production.env`. If all three passed:

```text
PIPELINE_SOURCES=nc_2026_general,ky_2026_general,mn_2026_general,ne_2026_general,tx_2026_general
PIPELINE_WORKERS=2
```

Preserve the deliberately chosen `PIPELINE_STATUS`; keep test data labelled test.
Run the existing pipeline service manually, inspect source-specific outcomes, and
verify the national CDN API has the new publications with no API caching. Test state
navigation, Governor tabs, MN counties, TX districts, and dashes for unknown counts.
Measure duration and memory with two workers before increasing concurrency.
Restart `election-pipeline.timer` and `election-backup.timer` after this manual run
passes. If only a subset passed, enable only that subset alongside NC/KY.

Take a new uploaded backup and run the established S3 download/isolated restore
check on that backup ID. It must include the new state tables and reader permissions.
Run the existing monitor dry-run and inspect `outcome: healthy` (its process exit code
alone is insufficient). Observe consecutive scheduled runs for each enabled source.
Repeat independent exact-count checks when the first real nonzero returns arrive.
Any failed live identity/reconciliation check pauses that source's promotion.

Migration 004 is additive. To pause a new source, remove its ID from scheduling and
investigate while retaining the last valid publication. Do not drop schemas or edit
the migration ledger. A full database rollback uses the established verified restore
procedure; changing a Git revision alone does not reverse migrations.
