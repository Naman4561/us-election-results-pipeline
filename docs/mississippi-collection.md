# Mississippi Senate collection

`ms_2026_general` collects ten official county reports every five minutes. It
does not collect a statewide Mississippi total. The other 72 counties are
explicitly unknown, including on the dashboard. This release covers the Senate
only; House results in the same source documents are outside this parser's scope.

## Supported sources

| Format | Counties | Changed reports |
| --- | --- | --- |
| Text Electionware PDF | DeSoto, Hancock, Hinds, Monroe, Pike | Automatically extracted and validated |
| Hart cumulative HTML | Rankin | Automatically extracted and validated |
| Official county news HTML | Neshoba | Automatically extracted and validated |
| Scanned PDF | Lauderdale, Madison, Warren | Exact reviewed file hash required; new bytes require review |

Source links, candidate identities, county FIPS codes, ballot-method limitations
and reviewed scan transcriptions are pinned in
`src/election_pipeline/resources/ms_2026_counties.json`. The parser verifies its
SHA-256 after canonicalizing CRLF to LF for Windows compatibility. County report
checksums always require the exact original bytes. The original offline captures are in
`tests/fixtures/mississippi/reports.zip`; the county research inventory is in
`docs/source-research/ms-2026-county-sources.json`.

The fresh collection check on November 5, 2026 at approximately 03:04 UTC reached
and parsed all ten configured reports. The three scans still matched the reviewed
files. Their report times remained November 3–4. A recent HTTP check means the
published file was rechecked; it does not mean the county issued new vote counts.
Accessibility from Lightsail must also be checked during `prepare`.

## Collection, parsing and storage

The reusable `collectors/county_files.py` backend schedules independent county
requests, with at most three requests in flight, 12-second socket timeouts and
per-file/cycle byte and time budgets. Different hosts supply the ten configured
files. It sends ETag or Last-Modified conditions when available. A valid 304
confirms the existing file; an unconditional 304 is rejected. There is one attempt
per county per collection cycle, including on 403 responses. No browser challenge
is bypassed. Source URLs are pinned, not discovered anew on each cycle; an official
URL change requires a reviewed configuration change.

The collector validates content before accepting it, even on HTTP 200. A failed
county request does not stop other county requests. A rejected changed file goes
to the existing quarantine directory. If a previously accepted county file exists,
its original bytes and last successful check are retained with `stale` status.
Without prior accepted bytes the county is `unavailable`, with no vote rows.
An entirely failed first collection fails rather than inventing a snapshot.

Each content-addressed raw ZIP contains ten original report members and
`county-status.json`, which registers all 82 counties. The regular raw, metadata,
parsed, run-report, backup and replay paths apply. The parser revalidates every
accepted member offline, emits `contest_results.csv`, `county_results.csv`,
`results.csv` and `county_feeds.csv`, and reconciles the accepted county rows to
their aggregate. The collector also revalidates prior accepted files before using
them as fallback data. Vote corrections may decrease counts; report generation
times may not go backwards.

Because check timestamps are captured, an unchanged set of vote reports can still
create a new bundle and publication. The live check's bundle was about 0.44 MB
compressed, roughly 0.13 GB of raw bundles per continuously running day at a five-minute
interval, before parsed files, database rows and backups. Retention and the poll
schedule should follow the project's operational policy; no cleanup of existing
production data is part of this change.

The additive `010_ms.sql` migration creates raw and published relations within
`ms`. dbt builds six models and checks county identity/coverage, candidate identity,
counts, reconciliation, percentages and provenance. The publication transaction
freezes county status with the same publication ID as vote rows. The reader can
select only the three published relations, including `ms.county_feed_status`.
Raw reports, intermediate tables and write access are not granted to it.

## Interpretation and dashboard

- The aggregate and percentages cover **Scott Colom, Cindy Hyde-Smith and Ty
  Pinkins in the accepted counties**. They are not statewide shares. All write-ins
  are excluded from this denominator and retained separately where supplied.
- Warren's label remains **Unadjudicated Write-In**. Unknown write-in tallies
  remain unknown. Blank precinct totals, such as Neshoba's, remain unknown.
- Monroe excludes absentee and affidavit ballots. Other known method limitations
  are recorded per county. All precincts reporting does not establish that all
  ballots have been counted.
- The national Senate map keeps Mississippi neutral and patterned as partial;
  it is excluded from national party-lead counts. The state map shows the 82
  counties, including uncollected and unavailable ones. County panels include
  source links, report times, last successful checks and scope notes.
- Counts from a failed source remain visible with a stale warning. Even if the
  pipeline stops entirely, the API derives stale status after 15 minutes without
  a successful county check. Monitoring alerts on configured stale/unavailable
  counties, including scan-review failures; the 72 uncollected counties are not
  operational failures.

## Reviewing a changed scan

1. Inspect the private run report/manifest for the county, rejected SHA-256 and
   quarantine path. Preserve those original bytes. Never replace the last accepted
   counts with automatic OCR output.
2. Visually review the county identity, election/date, Senate candidate names and
   parties, each count, write-in label and count, printed total, reporting
   precincts, generation time and ballot-method limitations. Record unknown fields
   as null; do not infer them from unrelated contests or precinct reports.
3. Add a new entry under that county's `reviewed_scans`, keyed by the exact file
   SHA-256. Retain prior entries so archived bundles remain replayable. Include
   the reviewed original in the test captures and add a known-answer test.
4. Recompute the policy SHA-256 in `parsers/ms.py`, preserve LF endings, and run
   the checks below before staging a new immutable release. A scan change has
   the same release/validation requirements as a parser change.

## Validation and rollout

Offline Python tests, JavaScript navigation tests, linting, dbt parsing and
PostgreSQL grammar checks can run without production access. The native PostgreSQL
acceptance test exercises loading, dbt validation, publication, reader grants and
failed-validation preservation of the live publication. Run it against a newly
created disposable database using the existing helper; it must not be skipped
before admission:

```powershell
.\.venv\Scripts\python -m pip install -e '.[postgres,transform,dashboard,deployment]' -r requirements-dev.txt
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_mississippi.py
```

After committing and pushing this code to the existing
`feat/complete-state-coverage` branch, stage that actual pushed commit using the
existing immutable-release script. Do not rebuild or restart the live Compose
stack. The stage script builds and tests the new image without production mounts.

Register `ms_2026_general` with `--status unofficial`, apply `010_ms.sql`, run
`prepare`, inspect the county results and provenance, then run `promote`. Promotion
creates a private publication. `activate-sources --source ms_2026_general` performs
the existing fresh backup, isolated restore, free-port candidate dashboard check
and atomic route/catalog switch, and admits the independent expansion worker.
It preserves the already active states. Do not enable it through the baseline
`PIPELINE_SOURCES` list.

After activation verify `/api/states/MS`, `/api/national` and the next scheduled
expansion cycle. Expect `vote_basis=covered_counties_named_candidates`, a complete
82-entry county register, `county_collection.complete=false`, and actual source
statuses rather than a hard requirement that all ten are always current.
The backup/restore gate must verify the new published provenance table and reader
permissions. No production activation has been performed by the local build.
