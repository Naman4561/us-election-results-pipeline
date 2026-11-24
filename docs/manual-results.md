# Manually acquired official result files

Manual acquisition replaces HTTP collection for one source. It keeps the same
state parser, raw archive contract, database tables, dbt checks, publication gate,
backup/restore gate and dashboard.

Seventeen states are imported this way: Arizona, Illinois, Kansas, Kentucky, Maine,
Michigan, Missouri, New Hampshire, New Jersey, Nevada, New York, Ohio, Oklahoma,
Pennsylvania, Tennessee, Wisconsin and Wyoming. Each is a source whose site refuses
automated clients, closed its feed, or posts results only county by county or as
documents. Which sources are manual is recorded in the production catalog
(`acquisition = "manual"`), not in `config/sources.toml`.

The importer takes one file per import, the source's `primary_filename`: a state's own
export (Ohio's `results.xml`, Michigan's `results.txt`, a PDF) or, for a state whose
results come in several files, one zip assembled from them (see
[multi-file imports](#multi-file-imports)). The file must state its report time, or the
operator declares the time the site showed (`--generated-at`).

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

A file that prints no report time (Michigan's export) is imported with the time the
state's site showed beside it, `--generated-at` (ISO, with its zone). The receipt records
it as `source_generated_basis=operator_declared`; a file that dates itself refuses one.

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
deploy register --source oh_2026_general             # a state not yet in the catalog
manual configure --source oh_2026_general            # --max-age-hours 6, --final
manual import --source oh_2026_general --file INBOX_FILE --sha256 HASH --downloaded-at UTC_TIME
manual promote --source oh_2026_general
```

`import` prepares a private candidate in the release image. Before promoting it,
spot-check candidate and county totals against the same official report. Then make it
public as any new state: an uploaded backup, `check-restore`, `deploy start-dashboard
--source oh_2026_general` and `deploy activate` with the restore report.

## Multi-file imports

Where a state's results come in several files, they are imported, unchanged, as one
`results.zip` whose parser reads every member. Two kinds:

- **Assembled as results come in, with a manifest:** Illinois (by election authority),
  New Jersey and Wisconsin (by county). The manifest (`manifest.json`, `sources.json`
  for New Jersey) names each member's address or "saved by the operator", its SHA-256,
  when it was saved and the report's own time; the parser refuses a member whose hash
  differs. Nevada's zip holds the results app's own `manifest.json` and the files it
  names, each checked against the SHA-256 it gives.
- **A fixed set of the state's files:** Maine's tabulation spreadsheets and ranked-choice
  summary, New Hampshire's four spreadsheets and Kentucky's seven saved results pages,
  each under the member name its parser expects.

Oklahoma's zip is not assembled: it is the state's own county results export.

Each import is the previous one with files added or replaced, so the zip is rebuilt
from the previous import and the new files:

- **Illinois:** `scripts/il_bundle.py pack --base <previous zip> --add <additions.json>`
  writes a deterministic zip (the same inputs give the same SHA-256), and `check` parses
  it offline as the pipeline would.
- **New Jersey:** `scripts/nj_bundle.py` fetches the counties that answer and adds the
  files saved by hand (`--saved county=path`).
- **The others** were packed by hand. Their published zips are archived by SHA-256, so
  each can be replayed, but not rebuilt by a tool in the repository yet (plan step 2.5,
  one generic pack tool).

A report no reader takes (a scan, a one-off layout) is transcribed into a review file
for that report's SHA-256 and signed by the operator who checked it against the report:
`il_bundle.py review` and `sign` write Illinois's; Wisconsin's `<County>.review.json`
and Mississippi's reviews are written by hand in the same spirit (plan step 2.4, one
review format and tool). The parser refuses an unsigned review or one for other files.

Every release replays each published manual source's last import under its new image
before it goes live, so a code change that breaks one of these parsers stops the
release rather than the next import.

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

## Certification

When the state certifies a manual source's results by labeling its own file official
(Oklahoma's board marked its county results export "OFFICIAL RESULTS"), check the totals,
then import that file as the certified file `certified-official-<primary_filename>`,
with the attestation and `--publish`:

```bash
manual import --source ok_2026_general --file INBOX_FILE --sha256 HASH --downloaded-at UTC_TIME \
    --certified-name certified-official-results.zip --publish \
    --certified-on 2026-11-10 --attested-by "NAME" --evidence-url https://oklahoma.gov/elections/...
```

It is read by the state's own parser after the feed's payload checks, published as
official with the attestation, and the source is marked final: no further import is due.
Oklahoma was certified this way on Nov 13, 2026, under the name `certified-county-results.zip`
(still read). A certificate published only as a document is transcribed instead
(`certified-results.csv`, see [deployment](deployment.md#operator-attested-certification)).

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
(Kansas stopped after November 6, 2026), configure the source with `--final`; a source
already public is marked with `manual final --source NAME` (Ohio, until it certifies).
The monitor then never reports the snapshot as overdue, but it still checks that the
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
