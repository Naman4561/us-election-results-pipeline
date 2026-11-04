# Georgia, South Carolina, Kansas, Ohio; Maine pending

This incremental change targets `04a74926593706596a2da9a2abbb1fc252343b4a`
(`fix/texas-civix`). It does not activate production coverage. Finish the Texas
source handoff before previewing this dashboard: existing public source contracts
must match the candidate image exactly.

| State | Scope | Collection | County results |
|---|---|---|---|
| GA | Senate, governor, House 01–14 (16 races) | One official JSON response | Aggregate only in this adapter |
| SC | Senate, governor, House 01–07 (9 races) | Official fallback index plus its newest dated XLSX report | Yes, including method reconciliation |
| KS | Senate, governor, House 01–04 (6 races) | One official HTML page, including embedded county tables | Reported counties; unreported counties stay unknown |
| OH | Senate special election, governor, House 01–15 (17 races) | One official IEEE/NIST XML response | Yes; separate statewide/district totals reconcile |
| ME | Senate, governor, House 01–02 planned | Official municipal sources still need discovery/validation | Pending |

There are 48 additional configured races across the four implemented states. The
same state feed supplies every House district, so including those districts adds
no result-download requests. Registered jobs enforce a five-minute minimum.
SC's report is a periodically posted fallback, not a promise of five-minute
source updates. Its original index, exact workbook, component URLs, final URLs,
hashes and response headers are retained. A recent collection/publication time
does not by itself prove the report contains newly counted votes. Review the
parser's `source_update_text` and `source_report_url` too.

Maine remains outside public coverage, as explicitly selected by the operator.
The Secretary of State describes municipal election-night reporting and later
submission to the state. No unverified statewide endpoint, media substitution,
blank publication, or fabricated zero totals are configured. Federal Maine
results also require an explicit first-choice/RCV distinction before ingestion.
Official reference:
https://www3.maine.gov/sos/news/general-election-two-months-away-secretary-states-office-provides-update-maine-election-1

## What changed and why

- Each format lives in `parsers/ga.py`, `ks.py`, `oh.py`, or `sc.py`; each state
  has its own landing and published tables. Existing migrations are unchanged.
- A small optional bundle resolver lives in `collectors/sc.py`. The shared
  downloader still enforces combined byte/time limits. Discovery reads only
  explicit, dated workbook links on the official page and accepts its expected
  host/path; no script execution or firewall workaround is used.
- New total-only dbt models use shared SQL macros with thin per-state models,
  tags, fixtures and completeness tests. A candidate build selects only its own
  state and uses a separate temporary schema.
- Candidate and eligible-county rosters are pinned in `parsers/scope_2026.py`.
  They are Python data intentionally included in the existing parser code hash.
  This catches dropped candidates/counties even when arithmetic would still
  reconcile, including removal of a zero-vote county. Legitimate roster/format
  changes require review; vote corrections do not require monotonic totals.
- Ohio governor/lieutenant-governor tickets produce one result per selection.
  Kansas's duplicated party-filter sections are ignored. Missing Kansas county
  reports are omitted from county results and appear grey, never as inferred zero.
- SC checks six method columns against each candidate total, county sums against
  official totals, and candidate sums against each county/race total. Its raw
  method detail is retained; this increment serves total votes only.
- Georgia verifies method sums and official race totals but starts with aggregate
  results to keep collection to one request. Its state view labels that limitation.
- The existing national/state dashboard now has four additional Census map files.
  Tabs preserve national/state selection and interaction uses the cached payload.
  Maine continues to show the existing not-covered state.

## Validation completed here

All four recorded official responses parsed and reconciled. A later collection
through the production collector code also parsed all four, including a new SC
workbook link and additional Kansas county returns. Fixtures are exact compressed
response bytes, with URL, byte count, SHA-256 and download-time provenance.

The Python suite, lint/format checks, dbt project parsing and dashboard interaction
checks pass. Embedded PostgreSQL SQL checks applied every migration, loaded the
recorded rows, ran all new transformation/check queries and share fixtures, copied
published results, verified reader restrictions, and detected injected county
corruption. This is not the native psycopg/dbt acceptance run: native PostgreSQL
integration tests are skipped in this environment and must pass below.

Internal agreement within one feed is not independent accuracy confirmation. Before
promotion, manually compare several candidate/race totals and county returns with
the corresponding official presentation at matching report versions/times.

## Laptop: apply and validate

Start in the repository on the Texas fix commit or a descendant, with a clean
working tree. Keep the patch in Downloads. Stop after any failure.

```powershell
git switch -c feat/five-state-expansion
$patch = "$env:USERPROFILE\Downloads\ga-sc-ks-oh-expansion.patch"
git apply --check $patch
if ($LASTEXITCODE -ne 0) { throw "Patch check failed." }
git apply $patch
if ($LASTEXITCODE -ne 0) { throw "Patch application failed." }
```

Install the existing project extras, then run the suite and the new native DB gate
against your local Docker PostgreSQL. `check_pipeline.py` creates and removes its
own disposable database; do not point it at production.

```powershell
.\.venv\Scripts\python -m pip install -e ".[postgres,transform,dashboard,deployment]" -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
.\.venv\Scripts\python -m ruff check .
if ($LASTEXITCODE -ne 0) { throw "Lint failed." }
.\.venv\Scripts\python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { throw "Formatting failed." }
.\.venv\Scripts\python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_five_states.py
if ($LASTEXITCODE -ne 0) { throw "Native PostgreSQL/dbt acceptance failed." }
npm ci
if ($LASTEXITCODE -ne 0) { throw "Dashboard dependency installation failed." }
npm test
if ($LASTEXITCODE -ne 0) { throw "Dashboard interaction checks failed." }
```

Review `git diff`, stage the changes, commit and push this new branch. Then build
from the pushed Git commit to preserve canonical source bytes across Windows/Linux.

```powershell
git add -A
git commit -m "feat: add official Georgia South Carolina Kansas and Ohio results"
if ($LASTEXITCODE -ne 0) { throw "Commit failed." }
git push -u origin feat/five-state-expansion
if ($LASTEXITCODE -ne 0) { throw "Push failed." }
$expansionCommit = (git rev-parse HEAD).Trim()
$expansionImage = "election-pipeline:$expansionCommit"
docker build --platform linux/amd64 --build-arg "VCS_REF=$expansionCommit" -t $expansionImage "https://github.com/Naman4561/us-election-results-pipeline.git#$expansionCommit"
if ($LASTEXITCODE -ne 0) { throw "Image build failed." }
$archive = Join-Path $env:TEMP "expansion-$expansionCommit.tar"
docker save -o $archive $expansionImage
if ($LASTEXITCODE -ne 0) { throw "Image export failed." }
scp -i "$env:USERPROFILE\Downloads\LightsailDefaultKey-us-east-2.pem" $archive "ubuntu@3.142.103.113:/tmp/five-state-expansion-image.tar"
if ($LASTEXITCODE -ne 0) { throw "Image upload failed." }
```

## Ubuntu: stage and admit privately

Use a new immutable checkout. Do not change the original worker's IMAGE_TAG or
PIPELINE_SOURCES, pull into its checkout, or restart its containers.

```bash
setupExpansionRelease() {
    expansionCommit="$(git ls-remote https://github.com/Naman4561/us-election-results-pipeline.git refs/heads/feat/five-state-expansion | cut -f1)"
    [[ "$expansionCommit" =~ ^[0-9a-f]{40}$ ]] || { printf 'Push feat/five-state-expansion first.\n'; return 1; }
    expansionRelease="/opt/election-releases/$expansionCommit"
    if [[ ! -d "$expansionRelease/.git" ]]; then
        sudo git clone --no-checkout https://github.com/Naman4561/us-election-results-pipeline.git "$expansionRelease" || return 1
    fi
    sudo git -C "$expansionRelease" checkout --detach "$expansionCommit" || return 1
    sudo python3 -m venv "$expansionRelease/.venv" || return 1
    sudo "$expansionRelease/.venv/bin/python" -m pip install -e "${expansionRelease}[postgres,deployment]" || return 1
    sudo docker load -i /tmp/five-state-expansion-image.tar || return 1
    controlRelease="$(sudo sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env)"
    [[ -x "$controlRelease/.venv/bin/python" ]] || { printf 'Existing deployment controls are missing.\n'; return 1; }
    sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" status || return 1
}
setupExpansionRelease
```

The installed isolated-deployment controls can dispatch these images without
upgrading the baseline worker. Verify catalog-aware backup/monitor controls are
installed and inspect `free -h` and `sudo docker stats --no-stream`. Keep the
existing resource caps initially. Each new source starts disabled and private.

```bash
prepareExpansionStates() {
    for item in ga:005_ga.sql sc:006_sc.sql ks:007_ks.sql oh:008_oh.sql; do
        stateCode="${item%%:*}"
        migrationFile="${item#*:}"
        sourceName="${stateCode}_2026_general"
        sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" register --release "$expansionRelease" --image "election-pipeline:$expansionCommit" --source "$sourceName" --status unofficial || return 1
        sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" migrate --source "$sourceName" --file "$expansionRelease/migrations/$migrationFile" || return 1
        sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" prepare --source "$sourceName" || return 1
    done
}
prepareExpansionStates
```

If interrupted, resume at the particular state's failed command; registration is
deliberately not repeatable for an already owned source. A busy backup or lock
timeout is a reason to let that job finish and retry that command. A 403/captcha,
new format/roster, incomplete data, or failed reconciliation leaves the state
private and requires diagnosis. Do not enable it to bypass the check.

Each preparation must end with `outcome=validated`. Inspect the source-specific
reports under `/srv/election-pipeline/data/runs/` and parser reports under
`data/parsed/`. Confirm election identity, expected race count, numeric totals,
roster coverage and reconciliation; independently compare official returns.

## Promote, recover, preview, activate

Once each source's accuracy checks pass, promote its prepared publication:

```bash
promoteExpansionStates() {
    for sourceName in ga_2026_general sc_2026_general ks_2026_general oh_2026_general; do
        sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" promote --source "$sourceName" || return 1
    done
}
promoteExpansionStates
```

All four are still private. Take a catalog-aware uploaded backup and run the
existing isolated S3 restore check. Capture its report without changing its JSON:

```bash
checkExpansionRestore() {
    sudo systemctl start election-backup.service || return 1
    backupId="$(sudo python3 -c 'import json; d=json.load(open("/srv/election-pipeline/backups/latest-backup.json")); assert d["outcome"]=="uploaded"; print(d["backup_id"])')" || return 1
    sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/production_recovery.py" check-restore --backup-id "$backupId" --catalog-file /var/lib/election-pipeline-deployment/catalog.json > /tmp/four-state-restore.json || return 1
    cat /tmp/four-state-restore.json
    restoreReport="$(python3 -c 'import json; d=json.load(open("/tmp/four-state-restore.json")); assert d["outcome"]=="passed"; print(d["report_path"])')" || return 1
}
checkExpansionRestore
```

The restore report must verify each of the four exact publication IDs, nonempty
published tables, writer/reader permissions, and removal of the temporary DB.
It must be less than 30 minutes old at activation. If the backup service was
already running, wait for it to finish and start a new backup after promotion.

Start a private dashboard candidate on an unused localhost port:

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" start-dashboard --source ga_2026_general --source sc_2026_general --source ks_2026_general --source oh_2026_general --image "election-pipeline:$expansionCommit" --port 8002 > /tmp/four-state-dashboard.json
cat /tmp/four-state-dashboard.json
candidateId="$(python3 -c 'import json; d=json.load(open("/tmp/four-state-dashboard.json")); print(d["id"])')"
curl -fsS http://127.0.0.1:8002/api/national
```

Preview using an SSH tunnel to remote `127.0.0.1:8002`. Check all nine supported
states, Senate/governor/House tab retention, county clicks for SC/KS/OH, the
aggregate-only GA label, and Maine's not-covered status. Then activate:

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" activate --candidate "$candidateId" --restore-report "$restoreReport"
sudo systemctl start election-expansion.service
sudo journalctl -u election-expansion.service -n 60 --no-pager -o cat
sudo systemctl start election-monitor.service
sudo cat /var/lib/election-pipeline-monitor/latest-check.json
```

Require `outcome=activated`, successful first scheduled runs and no new-state
monitoring issue. Unrelated legacy source errors remain separate work. Caddy
reloads to the checked candidate and retains the previous backend; the existing
collectors keep their pinned images. The worker runs admitted expansion sources
within its existing resource/cycle limits and prioritizes least-recently-run jobs.
Monitor deferrals and cycle duration before increasing capacity or concurrency.

If the newly activated dashboard fails, use the existing rollback:

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" rollback-dashboard
```

That restores the previous dashboard/catalog selection and disables only the newly
added coverage. Keep both active and previous dashboard containers until the
handoff is accepted. No production command in this document has been executed by
the assistant.
