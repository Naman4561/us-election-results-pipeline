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
| `python -m analysis fetch fec` | Downloads the FEC's 2026 bulk files (candidates, their totals, independent expenditures) into `finance`. Run again after the FEC updates them (post-general reports are due Dec 3); each download is kept beside the last. |
| `python -m analysis match` | Gives every candidate a key (`ref.candidates`) and links each Senate and House candidate to its FEC registration (`ref.candidate_ids`); lists any it cannot match. |
| `python -m analysis open` | Opens the workspace (a marimo notebook) in the browser. |
| `python -m analysis status` | Which backup the local copy is and which datasets are loaded. |

After `restore`, run `link` and `match` again (new publications may bring new spellings).

### Reviewing matches

`link` and `match` accept only unambiguous matches. The rest go in reviewed files,
committed with a note:

- `analysis/ref/county_aliases.csv`: `state,county,fips,note`.
- `analysis/ref/candidate_ids.csv`: `contest_id,choice,fec_id,note`, or `none` as the
  FEC ID for a candidate with no FEC registration (campaigns under $5,000 need none).
  Ashley Hinson is here: the FEC lists her as ARENHOLZ, ASHLEY HINSON.

A candidate is matched by surname within the same state, office and district (so Kevin
Hern's House registration is never his Senate one), by first name or initial only where
two share a surname, and two registrations reporting identical totals count once.

## The workspace

`workspace.py` is a [marimo](https://marimo.io) notebook. DuckDB attaches the local
database **read-only** as `elections`, so SQL cells read
`elections.serving.county_results` and the like, and nothing in the restored copy can be
changed by accident. Three views are ready:

- `county_results`: current results by county with FIPS codes, Governor, Senate and House.
- `contest_results`: current statewide (or district) totals with vote shares.
- `candidate_finance`: each Senate and House candidate's result beside their FEC totals
  (raised, spent, cash, individual money, self-funding) and the independent expenditures
  for and against them.

They read each state's one results source (`ref.current_publications`): some states keep
an older election-night source, and North Carolina a demographics dataset, beside it.

The table browser filters, sorts, groups and picks columns without code, and shows the
code it ran. A SQL cell's result is a dataframe for the next Python cell. Add cells and
save: the notebook is a starting point.

## Where the data lives

- **Local database** (`127.0.0.1:55432`, password in `~/election-analysis/.env`):
  - the restored pipeline schemas (`ops`, `landing`, `serving`, …): every snapshot and
    publication since Nov 3, not only the latest;
  - `ref`: `restores` (which backup the copy is), `datasets` (every outside file's URL,
    download time, hash and license), `counties`, `county_aliases`;
  - `ref.state_sources`, `ref.candidates`, `ref.candidate_ids`;
  - `finance`: `fec_candidates`, `fec_candidate_totals`, `fec_independent_expenditures`
    (as filed) and `independent_expenditures_in_effect` (amended filings replaced);
  - later: `census` (ACS), `history` (past results), `analytics` (dbt).
- **Downloads** under `~/election-analysis/raw/<dataset>/<sha256>`, kept as received.
- **Backups** under `~/election-analysis/backups/<backup_id>/database.dump`.

Set `ANALYSIS_HOME` to keep these elsewhere, and `ANALYSIS_SSH_HOST` if the server's SSH
alias is not `election-prod`.

## Limits to carry into findings

Results are unofficial unless `status` is `official`. Mississippi covers some counties
only; Vermont reports by town (not linked to counties yet); AK, CA, GA, HI, NE, RI and TX
publish statewide totals only; vote methods come from NC, VA, OK, IA, RI and HI only.
FEC totals run through the last report filed (Oct 14 before the post-general reports).
Sum outside spending over `finance.independent_expenditures_reviewed` where `counted`:
amended filings are replaced, a transaction filed again on a later report counts once,
and junk filers (any entry of $100 million or more; the 2026 file has three claiming
$38.5 billion) and reviewed exclusions (`analysis/ref/excluded_spenders.csv`) are left
out, each excluded row keeping its reason. Outside spending is by spender, not donor:
money given to a super PAC is spent in the PAC's name (contributions are not loaded yet).
Governor races are not in FEC data.

## Tests

```powershell
.venv-analysis\Scripts\python -m unittest discover -s analysis/tests -t .
```
