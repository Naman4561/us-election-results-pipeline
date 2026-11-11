# Manually acquired official result files

Manual acquisition replaces HTTP collection for one source. It keeps the same
state parser, raw archive contract, database tables, dbt checks, publication gate,
backup/restore gate and dashboard.

Ohio's official XML, Kansas's results page and Oklahoma's county results export are
imported this way. The importer accepts a configured single-file result bundle with an
ISO report timestamp.
Multi-file sources and adapters without that timestamp contract are rejected.

## Provenance and validation

The operator downloads the official file through a normal browser, records its UTC
download time and computes its SHA-256 before uploading it. On the host, place the
file under the existing data directory's `inbox/`, readable by container UID 10001.
The importer reads those bytes once and checks the original SHA-256. It creates a
deterministic ZIP containing the exact, unmodified file under the source's
`primary_filename` (`results.xml` for Ohio).

The full existing parser runs in a temporary directory before the archive is
committed. HTML challenges, wrong elections, missing roster/geography coverage,
inconsistent counts, future report timestamps and reports older than the previous
published manual report are rejected. Vote corrections may decrease totals.

Successful receipts use `outcome=manual_imported` and `acquisition_method=manual`.
HTTP status is NULL. The receipt retains the configured official URL, submitted
filename, original file hash/size, archive hash, parser code fingerprint, declared
download time, actual import time and the file's own generation time. An import does not
write the HTTP collector's state or pretend a successful source poll occurred.

The loader links this receipt to the snapshot in `ops.fetch_attempts`. Publication
uses the download time for first observation and the file's generation time for the
source modification timestamp. The metadata explicitly identifies the latter as
a document timestamp rather than an HTTP response header.

## Configuration and first publication

A manual source is a disabled, private source switched from HTTP polling to imports.
The worker polls only `acquisition = "http"` sources, so a manual one is never fetched.
Commands run from the current release (`R`, see [deployment](deployment.md)):

```bash
manual() { sudo "$R/.venv/bin/python" "$R/scripts/manual_source.py" "$@"; }
manual configure --source oh_2026_general            # --max-age-hours 6, --final
manual import --source oh_2026_general --file INBOX_FILE --sha256 HASH --downloaded-at UTC_TIME
manual promote --source oh_2026_general
```

`import` prepares a private candidate in the release image. Before promoting it,
spot-check candidate and county totals against the same official report. Then make it
public as any new state: an uploaded backup, `check-restore`, `deploy start-dashboard
--source oh_2026_general` and `deploy activate` with the restore report.

## A state added from its certified results

A state with no election-night source (one "awaiting certified results") is added
from its certified canvass alone. Its source is configured for manual imports as above,
and its first import is the state's certified file, named for the reader that checks it
(`certified-<format>`, see `parsers/certified.py`), with the operator's attestation:

```bash
deploy register --source wy_2026_general
manual configure --source wy_2026_general --final
manual import --source wy_2026_general --file INBOX_FILE --sha256 HASH --downloaded-at UTC_TIME     --certified-name certified-results.zip     --evidence-url https://sos.wyo.gov/Elections/Docs/2026/2026GeneralResults.aspx     --certified-on 2026-11-10 --attested-by "NAME"
manual promote --source wy_2026_general
```

The candidate is official and carries the attestation; the source's status becomes
official and no further import is due. It is made public like any new state (backup,
`check-restore`, `start-dashboard`, `activate`).

## Later updates

Once public, the operator can submit another official file through the same
import command with `--publish`. This runs the normal parser, load, dbt validation
and atomic publication. Failed input leaves the current publication untouched.
Identical input reuses the archive/release and retains the existing publication.
An import and a promotion for this source share a source-specific operation lock;
other sources continue independently.

A successful direct publication clears an older unpromoted candidate. Newer file
timestamps are required to move beyond the previous report timestamp; equal
timestamps are allowed for identical files or corrected reports. Counts do not
have to increase.

The dashboard labels a manual state `UNOFFICIAL · MANUAL SNAPSHOT`, shows the source report
timestamp, and says that results change on import. The national page identifies
the presence of manually updated states.

Monitoring checks the last successfully published file's declared download time
against a per-source operator-update limit (default six hours). It does not
require an HTTP poll or a periodic run. A rejected import does not refresh the manual freshness clock.

When the issuer announces that its unofficial results will no longer be updated
(Kansas stopped after November 6, 2026), configure the source with `--final`. The
monitor then never reports the snapshot as overdue, but it still checks that the
receipt matches the approved publication. A later import is still possible.

## Validation

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_manual.py
npm test
```

The native database test requires the disposable `*_test` database created by
`check_pipeline.py`. It covers receipt linkage, NULL HTTP status, observation and
source timestamps, dbt validation, publication and identical-file replay.
