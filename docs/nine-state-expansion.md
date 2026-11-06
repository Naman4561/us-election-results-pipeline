# Add Virginia, Idaho, Washington, Colorado, Iowa, Oregon, California, Maryland and Hawaii

| State | Senate | Governor | House | Races | County results | Vote methods (ballot type) |
| --- | :-: | :-: | --- | ---: | --- | --- |
| Virginia | ✓ | — | VA-01 to VA-11 | 12 | All 133 localities (95 counties, 38 cities) | **Stored**: early, Election Day, mailed absentee, provisional, post-election |
| Idaho | ✓ | ✓ | ID-01, ID-02 | 4 | All 44 counties | Not published |
| Washington | — | — | WA-01 to WA-10 | 10 | All 39 counties | Not published (all-mail) |
| Colorado | ✓ | ✓ | CO-01 to CO-08 | 10 | All 64 counties | Not published (all-mail; one "Total Votes" type) |
| Iowa | ✓ | ✓ | IA-01 to IA-04 | 6 | All 99 counties | **Stored**: Election Day, absentee (combined) |
| Oregon | ✓ | ✓ | OR-01 to OR-06 | 8 | All 36 counties | Not published (all-mail) |
| California | — | ✓ | CA-01 to CA-52 | 53 | Not collected (see below) | Not published |
| Maryland | — | ✓ | MD-01 to MD-08 | 9 | All 24 jurisdictions | Published, not stored: provisional is "NR" until the canvass |
| Hawaii | — | ✓ | HI-01, HI-02 | 3 | Not collected (PDF only) | **Stored**: mail, in person |

"—" means the state elects no one to that office in 2026; no race is invented for it.
Every byte comes from the state's own election office (or the vendor site it links
as its official results). No AP, news, aggregator, estimate or reconstructed value is
used.

**Montana is not included.** Its official site works and its federal pages parse, but
every page carries a visible "TEST" header banner, and its Statewide Export returns an
error page instead of the workbook on roughly half of all requests. Until Montana's
site drops the banner (or the Secretary of State confirms the figures are live), its
results cannot be validated as official returns, so they are not shipped. A draft
parser exists outside this release.

## Official sources

| State | Member | Source | Method |
| --- | --- | --- | --- |
| VA | `data.json` | `enr.elections.virginia.gov/results/public/api/elections/virginia/2026-November-General/data` | GET |
| VA | `item-va-*.json` | Same election's `ballot-items/<id>` for each selected contest | GET |
| ID | `data.json`, `item-id-*.json` | `results.voteidaho.gov/results/public/api/elections/id/nov2026/...` | GET |
| WA | `data.json`, `item-wa-*.json` | `results.votewa.gov/results/public/api/elections/washington/20261103/...` | GET |
| CO | `current_ver.txt` | `results.enr.clarityelections.com/CO/127407/current_ver.txt` | GET |
| CO | `summary.json`, `settings.json`, `detail.zip` | That version's `json/en/summary.json`, `json/en/electionsettings.json`, `reports/detailxml.zip` | GET |
| IA | same as CO | `results.enr.clarityelections.com/IA/127077/...`; detail is `reports/detailtxt.zip` (Iowa publishes no XML) | GET |
| OR | `export.html` | `results.oregonvotes.gov/ResultsExport.aspx` ("Statewide and County Exports") | GET |
| OR | `statewide.csv`, `governor.csv`, `county-*.csv` | That page's **Statewide Export**, **Governor Export** and 36 county links, with its own **CSV** option selected | POST (page's own form) |
| CA | `governor.json`, `us-rep-NN.json` | `api.sos.ca.gov/returns/governor`, `/returns/us-rep/district/N` | GET |
| MD | `governor.html`, `house-0N.html` | `elections.maryland.gov/elections/2026/general_results/gen_detail_results_2026_...html` ("County Break Down") | GET |
| HI | `summary.txt` | `elections.hawaii.gov/wp-content/results/2026 General/summary.txt` | GET |

Enhanced Voting election slugs (`2026-November-General`, `nov2026`, `20261103`) are the
ones each state's results site uses in its own page addresses; the parser also checks
each response's election date, production flag, jurisdiction and publication ID.

## Design decisions

**A vote-method contract any state can use.** NC and KY each have their own schema for
ballot types. This release generalizes the shared totals contract instead:
`parsers/totals.py: with_methods(methods)` returns the fields, load contract and row
converter for a state that also stores method columns. Every method count must be a
known nonnegative integer, and the methods must add up exactly to `total_votes` on every
statewide and county row; `finish()` checks this and that the stored methods match the
state's `State.methods`. The migrations add `NOT NULL` method columns with a
`sum = total_votes` check on all three tables. The dbt macros take an optional
`<state>_methods` variable: models carry the columns, county reconciliation compares
every method, and `<state>_vote_methods` rejects any row whose methods do not add up.
Total-only states are unchanged. The dashboard's existing method selector gains labels
for `post_election`, `mail` and `in_person`.

**Enhanced Voting (VA, ID, WA).** `collectors/enhanced_voting.py` GETs the election's
`data` document, asks the parser which contests it needs, and GETs exactly those
`ballot-items/<id>` documents (IDs must be UUIDs under the same election path). It then
fetches `data` again; if it changed, the site published new results mid-collection and
the attempt fails (retryably) instead of pairing statewide totals with another
publication's county detail. An unchanged `data` document reuses the previous bundle for
up to an hour. `parsers/enhanced_voting.py` is shared: election and contest identity,
candidate sums, every county's candidates summing exactly to the statewide counts, and
(Virginia) each candidate's five ballot groups adding up in every county and statewide.
Idaho and Washington must not carry method detail at all.

**Clarity (CO, IA).** `collectors/clarity.py` reads `current_ver.txt` and then GETs only
files under that version, so a bundle never mixes publications. Clarity's CDN sometimes
gzips JSON even when asked not to; the parser decompresses such a body within the
configured size budget. Colorado's detail XML is checked choice by choice against the
official summary (names compared with `'`/`"` folded, because the summary writes
`'Marky Jr'` where the detail writes `"Marky Jr"`; the detail spelling is stored).
Iowa's detail is a fixed-width text report read in its 30-character columns. Where a
county has no votes for a candidate, Iowa leaves Election Day and Absentee blank and
prints Total 0; only then is a blank read as 0 (two nonnegative counts adding up to 0
must both be 0). Every other blank, misaligned cell, row/total mismatch or summary
mismatch rejects the snapshot. Iowa's candidates have no IDs in the summary, so an ID is
the contest key and official list position, pinned by the roster.

**Oregon's visible exports, not hidden buttons.** Oregon's results pages contain
per-race export buttons styled `display: none`. They are not used. The collector uses
the public "Statewide and County Exports" page: it selects that page's own CSV radio
option (`enr_export.postback` now accepts radio choices, and only ones that exist on the
page with that exact value) and clicks the statewide, governor and 36 county links.
Oregon's pages change on every request, so each collection repeats all 38 exports; the
source polls every 15 minutes (`min_interval_seconds = 900`), about 28 seconds per
collection from the development network. Candidates nominated by several parties
("Democrat, Working Families") store the first party for map colors. Oregon reports
precincts as "0 of N" while mail ballots are counted, so precinct counts are not stored.

**California statewide only.** California's API serves one document per contest; county
results are one more document per county and contest (about 58 + 130 more requests at
roughly a second each), beyond the collector's 120-second budget and heavier than this
project should put on the state's API. Its results website is behind an Incapsula
JavaScript challenge, which the pipeline does not attempt. So California is collected
like Georgia and Texas: 53 statewide/districtwide documents every 15 minutes, no county
map. Candidates have no IDs; the published name is the ID, pinned by the roster.

**Maryland county pages.** For each contest the official "County Break Down" page lists
every jurisdiction and a Totals row; tables split "based on the number of candidates"
must list the same jurisdictions. County rows must add up exactly to Totals. Maryland's
summary pages split votes into Early Voting, Election Day, Mail-In and Provisional, but
provisional is "NR" until the canvass, so a method contract cannot hold yet and methods
are not stored (`vote_methods: reported_not_stored`).

**Hawaii statewide with methods.** `summary.txt` gives mail and in-person counts per
candidate; both are stored and must add up to the total. Hawaii publishes county results
only as PDFs, and its precinct file does not say which county a precinct is in, so county
detail is not collected (no precinct-to-county guessing). Governor tickets are stored as
"GREEN, Josh / KAWAKAMI, Derek S.K.".

**Other details.**
- PostgreSQL reserves `or`, so Oregon's migration quotes the schema (`"or"`), and test
  helpers that probe migrations quote schema names. Every runtime path already used
  quoted identifiers; `pg_dump --schema=or` and the dbt models were tested.
- Oregon's parser module is `parsers/oregon.py` (`State.parser_module`), since `or` is a
  Python keyword; its database schema stays `or`.
- Virginia (Fairfax County / Fairfax city) and Maryland (Baltimore County / Baltimore
  City) have counties and cities with the same base name, so their maps are keyed by the
  Census full name (`scripts/fetch_boundaries.py: FULL_NAME_STATES`). Maryland's
  "Saint Mary's" is stored as Census "St. Mary's County". Other maps were not
  regenerated; `us-map.json` regenerated byte-identically.
- The dashboard's statewide-only note no longer says methods are unavailable when a
  state (Hawaii) does publish them.
- Collector backends: VA/ID/WA `enhanced_voting`, CO/IA `clarity`, OR `enr_export`;
  CA, MD and HI use the standard bundle collector. OR and CA raise
  `max_zip_members` to 64 for their 39- and 53-member bundles.

## Validation in development

- `tests/fixtures/nine_states/<state>.zip` are bundles captured with the production
  collector and default settings on 2026-11-06 between 05:10 and 05:12 UTC;
  `provenance.json` records each component's URL, SHA-256, size, content type and
  method, and each archive's SHA-256. Candidate and county rosters in
  `resources/rosters_2026/` were reviewed from those captures.
- `tests/test_nine_states.py` parses, verifies, converts and replays all nine states;
  checks maps (unique county keys), dashboard payloads, method sums and national
  coverage; rejects altered county counts, methods that do not reconcile, unknown or
  unexpected methods, wrong elections and contests, dropped counties, summary/detail
  mismatches, misaligned or wrongly blank Iowa cells, Oregon county files that disagree
  with the statewide export, malformed California counts and Maryland totals; checks
  Oregon's radio-option validation; and covers the Enhanced Voting and Clarity
  collectors (only selected items, mid-update rejection, versioned paths only, reuse and
  refresh). Its database test applies every migration, then loads, validates (dbt),
  publishes and checks reader permissions for all nine states on a disposable
  PostgreSQL 16 database.
- Full suite: 327 Python tests and the dashboard interaction test pass; ruff and
  prettier are clean. The dashboard was rendered locally from the captures (VA early
  vote view, IA county view, OR, CA, MD, HI and the national House view).
- Production preparation must still pass from the server.

## Roll out on Ubuntu

No command below has been executed by the assistant. Push the release branch first.
Run each line separately. The new dashboard needs a free port; `8010` is used below
(check with `sudo ss -ltn | grep 80`).

```bash
CR=$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env); sudo bash "$CR/scripts/stage_coverage_release.sh" refs/heads/release/nine-states
CR=$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env); REV=${CR##*/}; echo "control root: $CR  revision: $REV"
```

Register, migrate and prepare each state privately. Each must end with
`outcome=validated`:

```bash
for item in va:014_va.sql id:015_id.sql wa:016_wa.sql co:017_co.sql ia:018_ia.sql or:019_or.sql ca:020_ca.sql md:021_md.sql hi:022_hi.sql; do s="${item%%:*}_2026_general"; echo "== $s"; sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" register --release "$CR" --image "election-pipeline:$REV" --source "$s" --status unofficial && sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" migrate --source "$s" --file "$CR/migrations/${item#*:}" && sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" prepare --source "$s" || break; done
```

If it stops, resume at that state's failed command (`register` is not repeatable for a
source it already owns; `migrate` reports `already_applied`). A fetch failure, a
changed roster or format, or a failed reconciliation leaves that state private and
needs diagnosis, not enabling. Oregon's and California's collections take about half a
minute each.

Inspect each parser report under `/srv/election-pipeline/data/parsed/<source>/`:
race count (12, 4, 10, 10, 6, 8, 53, 9, 3), `county_reconciliation` (`passed`;
`not_available` for CA and HI), `vote_methods` (`stored` for VA, IA, HI) and
`source_update_text`. Compare a few totals with the states' own pages. Then promote,
back up and verify the restore:

```bash
for s in va id wa co ia or ca md hi; do sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" promote --source "${s}_2026_general" || break; done
sudo systemctl start election-backup.service
backupId="$(sudo python3 -c 'import json; d=json.load(open("/srv/election-pipeline/backups/latest-backup.json")); assert d["outcome"]=="uploaded"; print(d["backup_id"])')"; sudo "$CR/.venv/bin/python" "$CR/scripts/production_recovery.py" check-restore --backup-id "$backupId" --catalog-file /var/lib/election-pipeline-deployment/catalog.json > /tmp/nine-states-restore.json; cat /tmp/nine-states-restore.json
restoreReport="$(python3 -c 'import json; d=json.load(open("/tmp/nine-states-restore.json")); assert d["outcome"]=="passed"; print(d["report_path"])')"
```

Start a private dashboard candidate and preview it through an SSH tunnel:

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" start-dashboard $(for s in va id wa co ia or ca md hi; do printf -- '--source %s_2026_general ' "$s"; done) --image "election-pipeline:$REV" --port 8010 > /tmp/nine-states-dashboard.json; cat /tmp/nine-states-dashboard.json
candidateId="$(python3 -c 'import json; print(json.load(open("/tmp/nine-states-dashboard.json"))["id"])')"; curl -fsS http://127.0.0.1:8010/api/national | head -c 300
```

If the first `curl` resets the connection, the container is still starting; retry after
a few seconds. Check each new state's tabs (Virginia's "Votes" selector, Iowa's and
Hawaii's methods, California and Hawaii as statewide totals, Washington with only the
House tab populated), county clicks, and that every existing state still renders. Then
activate, within 30 minutes of the restore report:

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" activate --candidate "$candidateId" --restore-report "$restoreReport"
sudo systemctl start election-expansion.service; sudo journalctl -u election-expansion.service -n 60 --no-pager -o cat
sudo systemctl start election-monitor.service; sudo cat /var/lib/election-pipeline-monitor/latest-check.json
```

## Rollback

- Before activation: remove the candidate dashboard container with
  `deploy.py discard-dashboard --name "$(python3 -c 'import json; print(json.load(open("/tmp/nine-states-dashboard.json"))["name"])')"`.
  The nine sources stay private; nothing public changed.
- After activation: `deploy.py rollback-dashboard` restores the previous dashboard and
  catalog selection and disables only the newly added coverage.
- The migrations are additive (`va`, `id`, `wa`, `co`, `ia`, `"or"`, `ca`, `md`, `hi`
  schemas only) and need no rollback.
- Existing sources keep their pinned images throughout.
