# Manually acquired official result files

Manual acquisition replaces HTTP collection for one source. It keeps the same
state parser, raw archive contract, database tables, dbt checks, publication gate,
backup/restore gate and dashboard. No existing migration changes are required.

The first supported use is Ohio's official 2026 general-election XML. The importer
accepts a configured single-file result bundle with an ISO report timestamp.
Multi-file sources and adapters without that timestamp contract are rejected.

## Provenance and validation

The operator downloads the official XML through a normal browser, records its UTC
download time and computes its SHA-256 before uploading it. On the host, place the
file under the existing data directory's `inbox/`, readable by container UID 10001.
The importer reads those bytes once and checks the original SHA-256. It creates a
deterministic ZIP containing the exact, unmodified XML under `results.xml`.

The full existing parser runs in a temporary directory before the archive is
committed. HTML challenges, wrong elections, missing roster/geography coverage,
inconsistent counts, future report timestamps and reports older than the previous
published manual report are rejected. Vote corrections may decrease totals.

Successful receipts use `outcome=manual_imported` and `acquisition_method=manual`.
HTTP status is NULL. The receipt retains the configured official URL, submitted
filename, original file hash/size, archive hash, parser code fingerprint, declared
download time, actual import time and XML generation time. An import does not
write the HTTP collector's state or pretend a successful source poll occurred.

The loader links this receipt to the snapshot in `ops.fetch_attempts`. Publication
uses the download time for first observation and the XML generation time for the
source modification timestamp. The metadata explicitly identifies the latter as
a document timestamp rather than an HTTP response header.

## Ownership and publication

Configure an existing disabled/private expansion source using a new immutable
release and image:

```text
scripts/manual_source.py configure --source oh_2026_general --release PATH --image IMAGE
```

Configuration verifies image/release identity, source/race identity, package
hashes and unchanged applied migrations. It changes only Ohio's owner to `manual`
and leaves it private. The existing recurring worker selects only owners of type
`expansion`, so publishing Ohio never enables HTTP collection for it.

The initial import prepares a private candidate:

```text
scripts/manual_source.py import --source oh_2026_general --file INBOX_FILE --sha256 HASH --downloaded-at UTC_TIME
scripts/manual_source.py promote --source oh_2026_general
```

Before promotion, spot-check candidate and county totals against the same official
report. Then take an uploaded catalog-aware backup and pass `check-restore`.
Start a dashboard candidate from the new image using `deploy.py start-dashboard
--source oh_2026_general` and activate it with the recent restore report. Use a
free localhost port, such as 8003 when the Georgia/SC dashboard occupies 8002.

The normal activation and rollback commands support manually owned result
sources. Baseline and already admitted automatic sources keep their existing
immutable images. Install the new versioned deployment/monitor controls before
configuring the manual source; never edit the active checkout or worker image.

## Later updates

After admission, the operator can submit another official XML through the same
import command with `--publish`. This runs the normal parser, load, dbt validation
and atomic publication. Failed input leaves the current publication untouched.
Identical input reuses the archive/release and retains the existing publication.
An import and a promotion for this source share a source-specific operation lock;
other sources continue independently.

A successful direct publication clears an older unpromoted candidate. Newer file
timestamps are required to move beyond the previous report timestamp; equal
timestamps are allowed for identical files or corrected reports. Counts do not
have to increase.

The dashboard labels Ohio `UNOFFICIAL · MANUAL SNAPSHOT`, shows the source report
timestamp, and says that results change on import. The national page identifies
the presence of manually updated states.

Monitoring checks the last successfully published file's declared download time
against a per-source operator-update limit (default six hours). It does not
require an HTTP poll or a periodic run from Ohio. Automatic states retain their
existing limits. A rejected import does not refresh the manual freshness clock.

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
