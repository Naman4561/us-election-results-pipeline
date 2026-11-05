# North Carolina analysis page

`/analysis.html` (linked from the dashboard header) analyses NC's published
returns. It is served by the existing dashboard process from the endpoint
`/api/analysis/nc`. It reads only published relations through the same
read-only, non-administrator reader as the map, so it needs no migration and no
new grant.

## What it shows

| Section                                                    | Published relations                                                                                                                      | Notes                                                                                                            |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| All races / race detail                                    | `nc.contest_candidate_results`, `nc.county_candidate_results` for the current publication                                                | Senate and every selected House district. Shares, margins and the vote-method mix by race, candidate and county. |
| How each method voted / how each candidate's votes arrived | Same rows, using `election_day`, `early_voting`, `absentee_by_mail`, `provisional`                                                       | Methods must sum to `total_votes`, and county rows must sum to contest rows, or the API refuses the snapshot.    |
| Count history                                              | `serving.publications` with the same source and status, plus each one's `contest_candidate_results`                                      | One point per published snapshot (at most 500). Downward corrections are shown as published.                     |
| Early and absentee ballots                                 | `nc.state_absentee_demographics`, `nc.county_absentee_demographics` for the current publication of `nc_absentee_early_demographics_2026` | Ballot counts by request type × party registration, race, sex and ethnicity. These are not candidate votes.      |

Every number is tied to a publication. The Sources panel shows each
publication's ID, status, publication and official-file times, source URL and
raw SHA-256. A source with no current publication is shown as missing, never as
zero. A lead is labelled as a lead, not a race call.

## Snapshot retention (unchanged by this page)

- **Raw files:** each distinct official file is stored once by SHA-256 under
  `raw/NC/2026-11-03/<source_id>/`. Every poll also writes a fetch-metadata
  record under `metadata/<source_id>/`.
- **Loaded data:** `ops.snapshots`, `ops.processing_releases`, `nc.results`
  (precinct rows) and `nc.absentee_demographics` keep every loaded release.
  The precinct table is not readable by the dashboard role.
- **Published data:** every publication's county, contest and demographic rows
  are kept; `serving.current_publications` only moves the pointer.
- **Pruning:** none in code, except local backup bundles
  (`scripts/prune_backups.py --keep 3`).

## Run locally

```powershell
.\.venv\Scripts\python -m election_pipeline.dashboard.server --demo
```

Open `http://127.0.0.1:8000/analysis.html`. Demo mode is synthetic and
labelled on the page. Against a database, configure the reader in
`.env.dashboard` exactly as for the map.

Checks: `tests/test_nc_analysis.py` covers known answers, integrity refusals,
demo labelling, and published history and demographics read through
`election_dashboard`. `tests/analysis.test.cjs` covers rendering, sorting and
filtering, missing publications, and rendering untrusted text as text.
