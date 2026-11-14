# Analysis workspace

Analyse the pipeline's results, with outside data, on this computer. Nothing here runs on
or queries production: the production image does not include this folder, and no release
check covers it.

## Setup (once)

Docker Desktop must be running (the local database is a PostgreSQL 17 container).

```powershell
python -m venv .venv-analysis
.venv-analysis\Scripts\python -m pip install -r analysis\requirements.txt
```

Run every command from the repository root with that environment's Python.

## Commands

| Command | What it does |
| --- | --- |
| `python -m analysis restore` | Streams the newest uploaded backup's `database.dump` from the server over SSH (read-only), checks it against the backup manifest's SHA-256, and restores it locally. Only the pipeline's schemas are replaced; outside data is kept. |
| `python -m analysis fetch counties` | Downloads the Census county list and loads `ref.counties` (FIPS codes). |
| `python -m analysis link` | Matches every county spelling in the results to its FIPS code (`ref.county_aliases`) and lists any it cannot match. |
| `python -m analysis open` | Opens the workspace (a marimo notebook) in the browser. |
| `python -m analysis status` | Which backup the local copy is and which datasets are loaded. |

After `restore`, run `link` again (new publications may bring new spellings).

## The workspace

`workspace.py` is a [marimo](https://marimo.io) notebook. DuckDB attaches the local
database **read-only** as `elections`, so SQL cells read
`elections.serving.county_results` and the like, and nothing in the restored copy can be
changed by accident. Two views are ready:

- `county_results`: current results by county with FIPS codes, Governor, Senate and House.
- `contest_results`: current statewide (or district) totals with vote shares.

The table browser filters, sorts, groups and picks columns without code, and shows the
code it ran. A SQL cell's result is a dataframe for the next Python cell. Add cells and
save: the notebook is a starting point.

## Where the data lives

- **Local database** (`127.0.0.1:55432`, password in `~/election-analysis/.env`):
  - the restored pipeline schemas (`ops`, `landing`, `serving`, …): every snapshot and
    publication since Nov 3, not only the latest;
  - `ref`: `restores` (which backup the copy is), `datasets` (every outside file's URL,
    download time, hash and license), `counties`, `county_aliases`;
  - later: `finance` (FEC), `census` (ACS), `history` (past results), `analytics` (dbt).
- **Downloads** under `~/election-analysis/raw/<dataset>/<sha256>`, kept as received.
- **Backups** under `~/election-analysis/backups/<backup_id>/database.dump`.

Set `ANALYSIS_HOME` to keep these elsewhere, and `ANALYSIS_SSH_HOST` if the server's SSH
alias is not `election-prod`.

## Limits to carry into findings

Results are unofficial unless `status` is `official`. Mississippi covers some counties
only; Vermont reports by town (not linked to counties yet); AK, CA, GA, HI, NE, RI and TX
publish statewide totals only; vote methods come from NC, VA, OK, IA, RI and HI only.

## Tests

```powershell
.venv-analysis\Scripts\python -m unittest discover -s analysis/tests -t .
```
