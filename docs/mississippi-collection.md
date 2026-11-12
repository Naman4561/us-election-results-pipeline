# Mississippi county collection

The pipeline can ingest all 82 Mississippi counties for the 2026 U.S. Senate race.
**13 official county sources are configured; 69 still need an official report.**
A reviewed-file intake path is not a claim of live coverage. Missing counties have
no vote rows, and the dashboard retains the partial-coverage warning.

## County sources

| Route | Counties | Update rule |
| --- | --- | --- |
| Text Electionware PDF | DeSoto, Hancock, Hinds, Monroe, Pike | Parse and reconcile each accepted response |
| Hart HTML | Rankin, Sunflower | Parse and reconcile each accepted response |
| Official county article | Neshoba | Strict county/election/candidate checks |
| Reviewed, exact-hash PDF | Lauderdale, Madison, Warren, Lafayette, Lamar | Poll the original URL; changed bytes require a new reviewed transcription |
| Reviewed file intake | Other 69 counties | Obtain an original official countywide report, review it, then explicitly import |

An automatic county whose site refuses the server (Hancock's Cloudflare front end
returns 403 to the AWS host) can take a reviewed copy of the report at its configured
URL through the same `import-county` command, but only while its feed has no accepted
response. Polling continues; failed polls keep the manual snapshot (and record
`poll_issue`), and the first accepted automatic response replaces it.

The first ten captures remain in `tests/fixtures/mississippi/reports.zip`.
The three additions and their original-byte fingerprints are in `expansion.zip`.
The canonical policy is `src/election_pipeline/resources/ms_2026_counties.json`.
The source/contact inventory lists all 82 counties in
`docs/source-research/ms-2026-county-sources.json`.

Lafayette excludes affidavits. Monroe excludes absentee and affidavit ballots.
Lamar's selected report is the 24-of-24 snapshot, not the sum of the earlier
8-of-24 and 16-of-24 reports. Its election header says November 3, 2026, but its
printed report clock says December 16, 2024. Its generation time is therefore
unknown; the dashboard explains that conflict. Only in-house absentees are
explicitly identified as included. Other absentee coverage remains unspecified.
Reporting all precincts does not establish that every ballot has been counted.

## Collection and recovery

The reusable `collectors/county_files.py` backend uses at most three concurrent
requests, 12-second socket timeouts, an 8 MB per-file limit, a shared 32 MiB
network budget and the configured cycle deadline. Deferred counties retain queue
priority for the next cycle. Limits allowing 83 ZIP members apply to these sources
only; other states retain their existing archive limits.

URLs are pinned. No discovery scan runs on every poll. There is one attempt per
configured county per cycle, with conditional requests where supported. Challenges
and malformed reports are rejected even when HTTP is 200. Rejected originals go
to quarantine. A failed county preserves its previously validated bytes and last
successful check; another county can still advance. One corrupt cached member
does not erase the other valid counties. Downward vote corrections are accepted;
report generation times may not regress. The explicit predecessor can seed the
replacement's cache without writing the predecessor's state.

Each replayable raw ZIP has 83 members: a manifest and one member slot per county.
Unknown slots are empty and have no accepted hash. Manual originals, their review,
source URLs, observation time and reviewer are retained with the same bundle.

Manifest check times change on every poll, so each poll is a new bundle (that is how
county freshness reaches each publication). From manifest version 3 the county
reports themselves are not embedded: each distinct report is kept once at
`members/<raw_sha256>.bin` beside the source's archives, and its slot in the ZIP is
empty. The parser reads the stored report, checks it against the manifest hash and
refuses a missing or altered one. Every distinct version of a report is retained;
a report polled unchanged all day is stored once, and a poll's bundle is a few KB.
Version 1-2 bundles (embedded reports, about 7 MB each) still parse unchanged and
are never rewritten, because database snapshots and backups verify their hashes.
An image older than this change refuses version 3 bundles rather than misreading
them, so after a rollback the first poll fetches every county again.

## Reviewed county intake

Find a current official countywide Senate report through the inventory's county
site or circuit clerk directory. An older election, precinct-only PDF, media total
or unofficial transcription is not an acceptable replacement. Preserve the original
bytes. Obtain the original through ordinary authorized access; the importer does
not bypass challenges or fetch URLs.

Create a review template locally, after collecting the source at least once:

```powershell
.\.venv\Scripts\python -m election_pipeline county-template --source ms_2026_general_all --county JACKSON --file C:\reports\jackson.pdf --output C:\reports\jackson-review.json
```

Fill every required field after inspecting the report: the three named candidates,
exact counts, countywide scope confirmation, official report/page URLs, reviewer,
and timezone-aware observation time. Unknown write-ins, precinct denominators and
report times remain null. Explain an unknown report date/time in `report_time_note`.
Known printed totals must reconcile. Do not infer turnout from candidate votes.
The template intentionally cannot be imported without review.

```powershell
.\.venv\Scripts\python -m election_pipeline import-county --source ms_2026_general_all --county JACKSON --file C:\reports\jackson.pdf --review-json C:\reports\jackson-review.json --data-dir data
```

These example paths are local. For production, copy the original and completed
review into the production data directory's `county-intake` folder, readable by UID
10001. The import runs in the current release image with the data mount, networking
disabled and no database credentials:

```bash
sudo "$R/.venv/bin/python" "$R/scripts/deploy.py" import-county --county JACKSON --file /srv/election-pipeline/data/county-intake/jackson.pdf --review-json /srv/election-pipeline/data/county-intake/jackson-review.json
```

The two paths must resolve inside the configured `DATA_DIR`. Adjust the example
paths if your existing data directory differs. If a source collection currently
holds its lock, retry after that cycle finishes. No timers need to be stopped.

Import holds the same per-source lock as collection. It makes no HTTP requests and
preserves other counties' status/timestamps and the source's `last_checked_at`.
Repeating the same original/review is a no-op; changing counts for the same file is
rejected. Re-checking a county and finding the same file is recorded by importing
it with the same review and a later `observed_at` (`outcome=manual_confirmed`): the
observation time moves forward, while counts and report time stay. A new corrected
original may reduce counts. A normal pipeline cycle
then parses, loads, runs dbt checks and publishes the imported bundle. Importing
alone never updates the public database.

Manual counties remain labelled **manual snapshot**, with their original
observation time, until another reviewed import. They are not confirmed by polling
other counties. After six hours the monitor sends a reminder (not an alert).
Automatic stale or unavailable sources alert separately; uncollected counties are not operational failures.

## Certification

When the Secretary of State certifies, its recap sheet (in 2024,
`2024ElectionRecapSheets.csv`) replaces the county collection. The recap lists every
county's Senate count with each candidate's statewide total; `parsers/ms.py`
(`parse_recap`, imported as `certified-recap.csv`) requires all 82 counties and the
three candidates with their parties, sums each candidate's counties to the printed
statewide total, and refuses anything else, including Senate write-in rows. Download it
in a browser, then import it as the source's certified file ([deployment](deployment.md)):

```bash
deploy certify --source ms_2026_general_all --certified-file INBOX_FILE \
    --certified-name certified-recap.csv --sha256 HASH --downloaded-at UTC_TIME \
    --certified-on 2026-11-13 --attested-by "NAME" --evidence-url https://www.sos.ms.gov/...
```

Collection pauses first and the source stays paused as official, so county changes
after certification are not collected. Every county then reads as current, from the
recap, and the manual-county reminders end. A recap in another layout is refused
rather than misread; its reader then needs the new layout.

## Database and presentation

Mississippi's vote rows go into the shared results tables like every state's. Its
county collection status has tables of its own (`ms` schema): dbt's `tag:ms` models and
test check the status register against the published bundle, and vote rows and county
status freeze under the same publication ID. The dashboard role can read the published
status only.

Aggregates and percentages cover the three named candidates in accepted counties,
not all statewide ballots. Write-ins remain separate, including Warren's original
`Unadjudicated Write-In` label. Mississippi stays neutral/patterned nationally and
is excluded from party-lead totals while coverage is partial or manual. Clicking
counties uses the already loaded API payload.

## Tests

The PostgreSQL acceptance test verifies the real 13-source bundle, reader permissions,
preservation after failed validation, and a separate 82-county synthetic capacity
bundle. Synthetic counts never go to production.

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_mississippi.py
```
