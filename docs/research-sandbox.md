# Election data catalog and analysis sandbox

The research catalog keeps exact source files, dataset metadata, and typed tables
in `data/research/catalog.sqlite3`. Each import creates an immutable version
under a dataset ID. The same files and manifest can be imported again safely.
Distinct snapshots remain selectable, and identical file bytes are stored once.

The sandbox runs separately from the results dashboard and binds to
`127.0.0.1`. It uses a local file and needs no production database credentials.

## Start the sandbox

From the repository root with your virtual environment active:

```powershell
python -m pip install -e ".[dashboard]"
python -m election_pipeline.research serve
```

Open [the local sandbox](http://127.0.0.1:8050). After each import, click
**Refresh**, select a dataset and version, and add filters as needed.
Click a table header or use **Sort by** for sorting. Choose a column under
**Statistics** for counts and missing values, numeric sum, mean and range,
or the ten most common text values. Statistics use all filtered rows.
Source details include downloads of the exact archived files.

## Register a file

Create a JSON manifest alongside your file. This is an illustrative template
for an annual county population table. Replace the metadata with the details
from your actual release.

```json
{
  "dataset_id": "us.census.county_population",
  "title": "County population estimates",
  "kind": "census",
  "status": "published",
  "jurisdiction": "US",
  "geography": "county",
  "grain": "One row per county and estimate year",
  "period_start": "2024-01-01",
  "period_end": "2024-12-31",
  "publisher": "U.S. Census Bureau",
  "source_url": "https://www.census.gov/programs-surveys/popest.html",
  "tags": ["population", "annual"],
  "column_types": {"county_fips": "text", "population": "integer"}
}
```

```powershell
python -m election_pipeline.research import county_population.csv --manifest county_population.json
python -m election_pipeline.research list
```

Use `--original <path>` to attach the upstream ZIP, PDF, codebook, or other
original to a normalized table. Repeat the option for multiple originals.
Use `--archive-only` to register a file such as a PDF or XLSX before a table
is available. Archives can be downloaded from the sandbox. To analyze the
contents, import a UTF-8 CSV or TSV as a tabular version.

| Metadata | How to use it |
| --- | --- |
| `dataset_id` | A stable lowercase ID for the dataset series. Keep it across revisions and releases at the same grain. Use a separate ID for a different table or grain. |
| `kind` | `election_results`, `demographics`, `census`, `participation`, `voter_registration`, or `other`. |
| `status` | `unofficial`, `certified`, `published`, `provisional`, `unknown`, or `synthetic`. Set this from the source. |
| `jurisdiction`, `geography`, `grain` | Coverage label such as `US` or `NC`, geography level, and what one row represents. |
| `period_start`, `period_end` | Coverage dates in YYYY-MM-DD. Use both for a range. |
| `election_date`, `source_as_of` | Optional election date and publisher snapshot timestamp. The timestamp requires a UTC offset. Import time is recorded separately. |
| `publisher`, `source_url` | Who published the data and where the source can be verified. |
| `tags`, `notes` | Optional search labels and caveats such as suppression rules or contest coverage. |
| `column_types` | Optional overrides by CSV header. Supported types are `text`, `integer`, and `number`. |

History is represented by the coverage dates. For example, a 2024 certified
result is an `election_results` version tagged `certified` with a 2024 election
date. Use `unknown` if certification status has not been confirmed.
For a release containing subtotals and detail, label the grain and filter
to comparable rows before summing.

### Column and query behavior

Empty cells are missing values. FIPS, GEOID, ZIP, code and ID columns default
to text, and values with leading zeros stay text unless the input is explicitly
converted before import. Other columns infer a type from all nonempty values.
Columns containing suppression markers or mixed text remain text. A declared
numeric type rejects an invalid value instead of converting it silently.
Numeric summaries ignore missing cells and use floating-point arithmetic
for sums and means.

The sandbox explores one version at a time. Filters combine with AND, text
equality is exact, and Contains is case-insensitive for ASCII. Missing
values sort last. It does not infer a join between datasets or combine
snapshots into a cumulative series.

## Bring in an existing pipeline snapshot

Choose a directory under `data/parsed/<source_id>/` that contains
`report.json`. Import one of its result or demographic CSVs:

```powershell
python -m election_pipeline.research import-parsed PARSED_DIRECTORY county_results.csv --jurisdiction NC --publisher "NC State Board of Elections" --status unofficial
```

For NC ballot demographics, choose `county_demographics.csv` or
`state_demographics.csv` in the demographic snapshot directory. The importer
verifies the CSV hash against the parser report and attaches the report to
the new version. It records the upstream original's SHA-256. If you have
that original locally, add `--original PATH` to store a verified copy.
Set `--status` from release provenance. Successful parsing alone does not
establish certification or publication. Add `--as-of` when you know the
official source timestamp.

## Store and access

- Pass `--store PATH` before the subcommand to use a different local catalog.
- Keep the database private, especially when working with individual voter
  records. Prefer aggregate registration and participation tables for shared analysis.
- Back up the SQLite database with SQLite's online backup command or after
  stopping the sandbox and any importer. A live database may have a WAL file.
- Imports accept up to 256 MiB per file and 256 columns. Split larger sources
  into documented logical tables or periods before importing.
- The local JSON API exposes `GET /api/catalog`, `GET /api/versions/{id}`,
  `POST /api/versions/{id}/query`, and per-version file downloads.
- This release provides file intake and analysis. New provider-specific
  collectors can hand normalized tables and their originals to the same importer.
