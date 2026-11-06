# Add South Dakota, New Mexico and Florida

| State | Senate | Governor | House | Races | County results | Vote methods (ballot type) |
| --- | :-: | :-: | --- | ---: | --- | --- |
| South Dakota | ✓ | ✓ | At-large | 3 | All 66 counties, every race | Not published |
| New Mexico | ✓ | ✓ | NM-01 to NM-03 | 5 | All 33 counties, every race; small counts masked | Export sheets exist; currently `*` (not reported) |
| Florida | ✓ (special) | ✓ | 27 of 28 districts | 29 | All 67 counties, every race | Not published statewide |

Every byte comes from the state's own election office. No AP, news, aggregator,
estimate or reconstructed value is used. Florida District 10 was uncontested, so it
was not on the ballot and is not in Florida's results; the House map shows it as
outside coverage.

## Official sources

| State | Member | Source | Method |
| --- | --- | --- | --- |
| SD | `results.csv` | `electionresults.sd.gov/resultsCSV.aspx?text=All&type=SWR&map=CTY` | GET |
| SD | `export.html` | `electionresults.sd.gov/ResultsExport.aspx` | GET |
| SD | `statewide.xlsx` | That page's **Statewide Export** button | POST (page's own form) |
| NM | `fed.csv`, `sw.csv` | `electionresults.sos.nm.gov/resultsCSV.aspx?text=All&type=FED` / `type=SW` | GET |
| NM | `fed.html`, `sw.html` | `electionresults.sos.nm.gov/resultsSW.aspx?type=FED` / `type=SW` | GET |
| NM | `county-nm-*.xlsx` | Each race's **County Level** export button on those pages | POST (page's own form) |
| FL | `results.txt` | `flelectionfiles.floridados.gov/enightfilespublic/20261103_ElecResultsFL.txt`, linked from Florida Election Watch's Downloads page | GET |

The statewide CSVs give each race's official totals, candidate IDs, parties and
precincts. The workbooks give one row per county. Florida's file has one row per race,
county and candidate with that county's precinct counts.

## Design decisions

**Collecting the official exports.** South Dakota and New Mexico publish county results
for statewide races only through export buttons on ASP.NET pages. The new
`collectors/enr_export.py` GETs the official page, finds exactly one export control
(by its label, or by the race ID from the official CSV), and posts that page's hidden
form fields back to the same page, exactly as the button does. New Mexico's site needs
the session and anti-forgery cookies it sets on that page, so the collector keeps a
cookie jar for one collection only. It never persists cookies, solves a challenge,
changes its user agent or retries around a block. A response that is not a workbook is
a fetch failure; the last publication stays live. `fetch_file` gained two optional
keyword arguments (`form`, `cookies`); without them it behaves exactly as before.

**Avoiding needless snapshots.** Each workbook embeds its download time, so identical
results would hash differently every poll. If every statewide CSV is byte-identical to
the previous bundle's and that bundle is less than an hour old, the previous bundle is
returned unchanged (no page fetches or posts). Otherwise everything is fetched again,
so a county-only correction with unchanged statewide totals appears within an hour.
South Dakota's CSV restamps itself about every 15 minutes, so it refreshes at that pace.

**Reconciliation.**
- South Dakota and New Mexico: every county row, the workbook's TOTALS row and the
  statewide CSV must agree exactly per candidate. Candidate names are matched after
  normalizing whitespace and quote marks (SD's CSV writes `'Nikki'`, its workbook
  `"Nikki"`).
- Florida publishes no separate statewide row; each statewide total is the sum of the
  official county rows (Florida Election Watch's statewide pages show the same sums).
  Every county must list every candidate, with consistent precinct counts.
- All three: election identity checks, pinned candidate and county rosters in
  `resources/rosters_2026/` (from the recorded captures), and dbt release-completeness,
  candidate-count and county-reconciliation tests.

**New Mexico's voter-privacy masking.** New Mexico prints `*` instead of a small county
count to protect ballot secrecy ("Voter Privacy Masking" on its election-stats site). In
the November 6 capture this happens once: Chaves County's slice of NM-02, where
Cunningham shows 65 votes and Vasquez `*`. The pipeline never reconstructs a masked
count, even though subtraction could. Instead:

- the parser leaves that county out of that race's county rows entirely, so the map
  never shows a one-candidate county result;
- it still checks every printed number exactly: TOTALS equals the sum of printed cells,
  equals the statewide CSV for every candidate with no masked cell, and may only be
  lower for a candidate with one;
- the statewide total remains New Mexico's official CSV figure;
- `nm_county_reconciliation` (dbt) requires county sums to be at most the official
  totals instead of equal;
- the dashboard accepts a shortfall only for a state with `county_privacy_masking`, and
  only when no candidate's county sum exceeds its official total. The race then says
  that New Mexico hides some small counts and that its counties add up to less than the
  total. A masked county shows "hidden for voter privacy", not "retry shortly".

Every other state's dashboard and dbt checks are unchanged.

**Vote methods.** South Dakota's site and Florida's statewide file have none. Florida's
67 county supervisors publish method splits on separate county systems, which would be a
Mississippi-sized project. New Mexico's County Level export includes Absentee, Early and
ElectionDay sheets, but they are all `*` with zero totals today. The parser records
`vote_methods: not_reported`. If New Mexico fills them, each county's methods must add up
to its total or the snapshot is rejected, and the report says `reported_not_stored`.
Storing them needs a vote-method contract (schema, dbt and dashboard columns, as for NC
and KY), deliberately left for when real numbers exist to build and test against.

**Other details.**
- Florida lists governor tickets with the governor in the first-name field and the
  lieutenant governor in the last-name field, separated by `/`. They are shown as
  "Jolly / Graham". Named write-in candidates (party `WRI`) are kept as candidates.
- Florida's file has no report time; the parser does not invent one.
- New Mexico's export spells "Dona Ana"; Census spells "Doña Ana".
  `scripts/fetch_boundaries.py` now folds accents when building county keys. The
  existing state maps were not regenerated; `us-map.json` regenerated byte-identically.

## Validation in development

- `tests/fixtures/new_states/` holds the exact official responses captured on
  2026-11-06 at about 00:51 UTC by the production collector code, with URL, method,
  SHA-256, size and content type in `provenance.json`.
- `tests/test_new_states.py` parses, verifies, converts and replays each state; checks
  maps, dashboard payloads and national coverage; rejects altered counts, wrong
  elections, mismatched TOTALS, bad masks, unreconciled vote methods, duplicate or
  missing Florida rows, non-workbook exports and forms posting elsewhere; accepts valid
  downward corrections; covers collector reuse and refresh; and posts a real form with
  cookies to a loopback server. Its database test loads, validates (dbt), publishes and
  checks reader permissions for all three states on a disposable PostgreSQL database.
- Full suite: Python and dashboard interaction tests pass; see the commit message.
- The collector was run against the three live official sites from the development
  network. Production preparation must still pass from the server.

## Roll out on Ubuntu

No command below has been executed by the assistant. Push this branch first. Run
each line separately.

```bash
CR=$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env); sudo bash "$CR/scripts/stage_coverage_release.sh" refs/heads/feat/sd-nm-fl
CR=$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env); REV=${CR##*/}; echo "control root: $CR  revision: $REV"
```

Register, migrate and prepare each state privately. Each must end with
`outcome=validated`:

```bash
for item in sd:011_sd.sql nm:012_nm.sql fl:013_fl.sql; do s="${item%%:*}_2026_general"; sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" register --release "$CR" --image "election-pipeline:$REV" --source "$s" --status unofficial && sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" migrate --source "$s" --file "$CR/migrations/${item#*:}" && sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" prepare --source "$s" || break; done
```

If it stops, resume at that state's failed command; `register` is not repeatable for a
source it already owns. A 403 or challenge, a changed roster or format, or a failed
reconciliation leaves the state private and needs diagnosis, not enabling.

Inspect each parser report under `/srv/election-pipeline/data/parsed/<source>/`:
election identity, race count (3, 5, 29), `county_reconciliation`
(`passed`, `passed_except_privacy_masked_counties`, `not_available` for Florida's
county-sum totals), `privacy_masked_counties` and `vote_methods`. Compare a few totals
with the states' own pages. Then promote, back up and verify the restore:

```bash
for s in sd_2026_general nm_2026_general fl_2026_general; do sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" promote --source "$s" || break; done
sudo systemctl start election-backup.service
backupId="$(sudo python3 -c 'import json; d=json.load(open("/srv/election-pipeline/backups/latest-backup.json")); assert d["outcome"]=="uploaded"; print(d["backup_id"])')"; sudo "$CR/.venv/bin/python" "$CR/scripts/production_recovery.py" check-restore --backup-id "$backupId" --catalog-file /var/lib/election-pipeline-deployment/catalog.json > /tmp/sd-nm-fl-restore.json; cat /tmp/sd-nm-fl-restore.json
restoreReport="$(python3 -c 'import json; d=json.load(open("/tmp/sd-nm-fl-restore.json")); assert d["outcome"]=="passed"; print(d["report_path"])')"
```

Start a private dashboard candidate and preview it through an SSH tunnel to
`127.0.0.1:8002`:

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" start-dashboard --source sd_2026_general --source nm_2026_general --source fl_2026_general --image "election-pipeline:$REV" --port 8002 > /tmp/sd-nm-fl-dashboard.json; cat /tmp/sd-nm-fl-dashboard.json
candidateId="$(python3 -c 'import json; print(json.load(open("/tmp/sd-nm-fl-dashboard.json"))["id"])')"; curl -fsS http://127.0.0.1:8002/api/national | head -c 300
```

Check each new state's Senate, governor and House tabs, county clicks, NM-02's
privacy note, Florida District 10 shown as outside coverage, and that every existing
state still renders. Then activate, within 30 minutes of the restore report:

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" activate --candidate "$candidateId" --restore-report "$restoreReport"
sudo systemctl start election-expansion.service; sudo journalctl -u election-expansion.service -n 60 --no-pager -o cat
sudo systemctl start election-monitor.service; sudo cat /var/lib/election-pipeline-monitor/latest-check.json
```

## Rollback

- Before activation: remove the candidate dashboard container with
  `deploy.py discard-dashboard --name "$(python3 -c 'import json; print(json.load(open("/tmp/sd-nm-fl-dashboard.json"))["name"])')"`.
  The three sources stay private; nothing public changed.
- After activation: `deploy.py rollback-dashboard` restores the previous dashboard and
  catalog selection and disables only the newly added coverage.
- The migrations are additive (`sd`, `nm`, `fl` schemas only) and need no rollback.
- Restore the previous controls with `deployment.env.before-coverage.$REV` and the old
  release's `install-controls` if needed. Existing sources keep their pinned images
  throughout.
