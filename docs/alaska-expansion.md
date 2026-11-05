# Alaska expansion

Scope: `AK-SEN`, `AK-GOV`, and `AK-AL`. These are statewide totals for the candidates listed in the official first-choice CSV. The public interface labels both this candidate scope and Round 1. It does not import or imply subsequent ranked-choice tabulations.

## Source and collection

- CSV: https://www.elections.alaska.gov/enr/26GENR/results/GA_ENR_Precinct_State_of_Alaska.csv
- Election identity page: https://www.elections.alaska.gov/election-results/e/?id=26genr
- Two bounded requests per collection, with a minimum interval of 300 seconds. No published rate-limit guarantee was found.
- Both original files are archived together. A failed request cannot reuse an older good file as the current observation.
- The collector's default time, size, retry, locking, and metadata policies are unchanged.

Alaska's server presented the wrong intermediate certificate during the Lightsail probe. The packaged `alaska-issuers.pem` contains two **public issuer certificates**, with provenance in `alaska-issuers.json`. The host-specific TLS context checks the bundle hash, retains hostname/certificate verification, and disables partial-chain trust. It must reach a root already trusted by the platform. Other source hosts use their existing transport. There are no new system trust-store entries, insecure requests, or runtime certificate downloads.

## Reviewed capture

The uploaded capture is 3,307,857 bytes, with SHA-256:

```text
8f70acd9d671c46d3ee414bbe554ce51eae5f4826bc90e9bc553cd19c96f9c08
```

Its original 20,046 records are retained in a compressed regression fixture. The fixture's HTML identity page is explicitly synthetic; it is not evidence of a separately captured official page or an independent result comparison.

| Contest | Selected records | Reporting units | Listed candidate votes | Ballot residual |
|---|---:|---:|---:|---:|
| AK-SEN | 2,092 | 523 | 213,180 | 721 |
| AK-GOV | 2,088 | 522 | 210,789 | 656 |
| AK-AL | 2,092 | 523 | 212,455 | 420 |

`Pct_seq_nbr` identifies reporting units. Eighty units have blank `Pct_Id` values; dropping them would lose early-vote/questioned units. The federal-only overseas unit `99-999` is present for Senate and House and absent for governor. This is preserved in the per-contest roster.

The parser pins candidate IDs, raw names/party codes, reporting units, and contest identities from this capture. Missing units/candidates are rejected even when counts are zero. New units or changed candidate identities require a reviewed scope update. The scope hash is pinned in the parser, so a scope change also changes parser identity.

The two Sullivan candidates remain separate. The blank party for `Sullivan, Daniel J. Jr.` remains SQL NULL; it is not filled from a name lookup. `DEM/NON` is retained for the governor ticket and displayed as published; its map category follows the Democratic gubernatorial ticket.

## Reconciliation and interpretation

All five vote-method components must sum to each row's total ballots, votes, undervotes, and overvotes. Candidate rows in the same reporting unit must agree on ballot metadata. Listed candidate votes plus under/overvotes cannot exceed that unit's ballots, for either the overall count or an individual voting method.

Ballot totals are counted once per unit, not once per candidate. The CSV has no write-in category for these contests. The positive residual `ballots - listed candidate votes - undervotes - overvotes` is recorded in the parser report without assigning it to write-ins, a candidate, or a party. Its cause has not been independently established.

`contest_votes` and `vote_share` describe **listed candidate votes**, not all ballots or all valid choices. No synthetic candidate rows are created to fill the residual. `Reporting_flag` is retained in diagnostics without treating it as a vote-count percentage. Downward corrections and all-zero snapshots are allowed when identity and arithmetic checks still pass.

The full CSV retains method/precinct detail for later analytics. Version 1 loads 12 aggregate candidate rows into `ak.results` and publishes to `ak.contest_candidate_results`. Per-precinct and per-candidate method details remain in the immutable raw archive; method totals and ballot residuals are also in `ops.processing_releases.parser_report`.

## Database and dashboard

`009_ak.sql` creates only the `ak` schema, result tables, and read-only dashboard grants. It does not alter existing state tables. The dbt models reuse the shared total-only contract, with state-tagged completeness, uniqueness, count, share, and unit tests.

The Alaska state view reuses the existing reviewed Census outline and at-large House geometry. It does not map reporting units to boroughs. Senate and governor therefore show one state shape; House shows `AK-AL`. National/state tabs retain location. Tooltips, totals, and notices identify the first-choice/listed-candidate scope.

## Local validation and rollout

Apply the incremental patch on `feat/complete-state-coverage`. Run the suite and the disposable PostgreSQL check before committing:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_alaska.py
```

The second command requires your existing local PostgreSQL setup and `.env`. It creates and removes a disposable `_test` database, exercises dbt/publication, and verifies that the reader can access published results but not raw tables. Do not point `ELECTION_TEST_DATABASE` at production.

Commit and push to `feat/complete-state-coverage`. From Ubuntu, use the existing staging script from a fresh temporary checkout so that the new image is built/tested before control files change:

```bash
stageCheckout="$(mktemp -d /tmp/election-alaska.XXXXXXXX)"
git clone --depth 1 --branch feat/complete-state-coverage https://github.com/Naman4561/us-election-results-pipeline.git "$stageCheckout"
sudo bash "$stageCheckout/scripts/stage_coverage_release.sh"
```

Each command must succeed before continuing. The script reads the pushed commit itself, creates an immutable release, builds the pinned image, runs the offline Linux suite, then installs versioned maintenance controls. Existing collectors and the public dashboard remain running.

Define the helper again if this is a new SSH session:

```bash
deployElection() {
    local controlRelease
    controlRelease="$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env)" || return 1
    sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" "$@"
}
```

Then prepare Alaska privately:

```bash
alaskaRelease="$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env)"
deployElection register --release "$alaskaRelease" --image "election-pipeline:${alaskaRelease##*/}" --source ak_2026_general --status unofficial
deployElection migrate --source ak_2026_general --file "$alaskaRelease/migrations/009_ak.sql"
deployElection prepare --source ak_2026_general
deployElection promote --source ak_2026_general
```

The live preparation must pass the actual election-page/CSV checks. Preparation validates; promotion creates a private publication. Neither enables the state in the public catalog.

Independent agreement with another official report has **not** been established by the regression fixture. Before public activation, compare the listed candidate totals with the official summary PDF for the same source update. Account separately for any unlisted categories; do not compare the listed-candidate percentage denominator with an all-choice denominator. The official PDF link observed on the election page is:

https://www.elections.alaska.gov/enr/26GENR/results/ElectionSummaryReportRPT.pdf

After that accuracy check, activate with:

```bash
deployElection activate-sources --source ak_2026_general
```

`activate-sources` is reusable for later registered expansion sources. It requires a private publication, uploads a fresh backup, performs an isolated restore/permission check, verifies the restored publication and table inventory, chooses an unused localhost dashboard port, checks prospective coverage, and invokes the existing atomic catalog/Caddy handoff. Existing coverage is retained. It does not stop or rebuild the baseline containers. Existing handoff/rollback safeguards still apply.

Refresh the public page after activation and check Alaska's labels and all three tabs. Run monitoring after the next scheduled collection:

```bash
sudo "$alaskaRelease/.venv/bin/python" "$alaskaRelease/scripts/monitor_production.py" --dry-run
```

If rollback is needed, `deployElection rollback-dashboard` uses the existing checked prior route/catalog. Registered raw data and private publications remain available for investigation.

## Mississippi

Mississippi remains unconfigured. The official Secretary of State results page inspected on November 4 lists the March 2026 primaries but no verified statewide general-election feed:

https://www.sos.ms.gov/elections-voting/election-results

Its intended coverage remains Senate plus House districts 1–4. A county-based implementation would first need a verified official-source inventory and clear statewide completeness rules. No primary results, partial county sum, or third-party count has been substituted for statewide general-election totals.
