# Switch Texas to the verified Civix JSON response

The operator's saved response identifies election date `11032026`, version
`enr/53815/9/`, and all seven watched races. In that snapshot, Senate totals were
153,561, governor totals 134,265, and TX-28 totals 20,440; the other four watched
House races had explicit numeric zeros. Candidate votes matched each race's total.
These are observations of that snapshot, not current totals or completion estimates.

The parser decodes Home/Federal/StateWide, validates the election ID/date, checks
all candidate votes against reported race totals, and preserves write-ins and
Unicode names. It rejects missing/duplicate races, duplicate JSON keys/candidates,
malformed base64, null/blank/negative/fractional votes and inconsistent totals.
The update text has no stated timezone, so it is preserved without guessing one.

No SQL migration or dashboard layout change is required. The new parser writes the
existing total-only contract. Legacy HTML parsing remains available when replaying
with the old source contract. The new collector uses one JSON download per check,
with the existing five-minute interval.

## Laptop: apply, test, commit, build

Work in the repository with the deployment changes already applied. Apply the small
Texas patch from Downloads. Stop if any command fails.

```powershell
git switch -c fix/texas-civix
$patch = "$env:USERPROFILE\Downloads\texas-civix-fix.patch"
git apply --check $patch
if ($LASTEXITCODE -ne 0) { throw "Patch check failed." }
git apply $patch
if ($LASTEXITCODE -ne 0) { throw "Patch application failed." }
.\.venv\Scripts\python -m unittest discover -s tests -p test_texas_civix.py -v
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
```

Linux-only handoff tests are skipped on Windows and run in GitHub CI. Stage the
Texas changes explicitly, then commit and push:

```powershell
git add config/sources.toml src/election_pipeline/parsers/tx.py src/election_pipeline/parsers/tx_civix.py src/election_pipeline/states.py src/election_pipeline/deployment/control.py scripts/deploy.py scripts/check_texas_civix.py tests/test_expansion.py tests/test_texas_civix.py docs/texas-civix-fix.md
git commit -m "fix: collect Texas Civix results and isolate its worker"
git push -u origin fix/texas-civix
```

Build on the laptop from the pushed commit, preserving canonical Git file bytes.
Use the working SSH key and host from this deployment:

```powershell
$texasCommit = (git rev-parse HEAD).Trim()
$texasImage = "election-pipeline:$texasCommit"
docker build --platform linux/amd64 --build-arg "VCS_REF=$texasCommit" -t $texasImage "https://github.com/Naman4561/us-election-results-pipeline.git#$texasCommit"
if ($LASTEXITCODE -ne 0) { throw "Image build failed." }
$imageArchive = Join-Path $env:TEMP "texas-civix-$texasCommit.tar"
docker save -o $imageArchive $texasImage
if ($LASTEXITCODE -ne 0) { throw "Image export failed." }
scp -i "$env:USERPROFILE\Downloads\LightsailDefaultKey-us-east-2.pem" $imageArchive "ubuntu@3.142.103.113:/tmp/texas-civix-image.tar"
```

## Ubuntu: stage a new immutable checkout

The function obtains the actual pushed commit. It stops on failure and leaves the
existing `/opt/election-pipeline` checkout and its Docker tag untouched. Run only
after the laptop push and image transfer have succeeded.

```bash
setupTexasRelease() {
    texasCommit="$(git ls-remote https://github.com/Naman4561/us-election-results-pipeline.git refs/heads/fix/texas-civix | cut -f1)"
    [[ "$texasCommit" =~ ^[0-9a-f]{40}$ ]] || { printf 'Push fix/texas-civix first.\n'; return 1; }
    texasRelease="/opt/election-releases/$texasCommit"
    if [[ ! -d "$texasRelease/.git" ]]; then
        sudo git clone --no-checkout https://github.com/Naman4561/us-election-results-pipeline.git "$texasRelease" || return 1
    fi
    sudo git -C "$texasRelease" checkout --detach "$texasCommit" || return 1
    sudo python3 -m venv "$texasRelease/.venv" || return 1
    sudo "$texasRelease/.venv/bin/python" -m pip install -e "${texasRelease}[postgres,deployment]" || return 1
    sudo docker load -i /tmp/texas-civix-image.tar || return 1
    printf 'Texas release ready: %s\n' "$texasRelease"
}
setupTexasRelease
```

## Validate the actual saved response

Run the offline checker against the original saved file. It creates temporary
archives/output, never calls the network or database, and returns a report. The
local automated fixtures were derived from the pasted summary; this step checks
the full original 1.6 MB response, including any fields absent from that summary.

```bash
set -o pipefail
sudo "$texasRelease/.venv/bin/python" "$texasRelease/scripts/check_texas_civix.py" --file /tmp/texas-civix-probe.txt | sudo tee /tmp/texas-civix-validation.json
```

Require `outcome=parsed`, `contest_count=7`, `all_counts_zero=false`, and both
reconciliation fields equal to `passed`. Review the printed candidate totals
against the saved-response summary. Stop here if the actual file is rejected.

## Transfer only Texas

Prerequisites: the deployment catalog exists, and `install-controls` has installed
the catalog-aware backup/monitor overrides and the expansion units. The handoff
refuses to proceed without these. It also refuses while the baseline cycle or a
backup/catalog operation is running. Let that job finish and retry; do not stop it.

Enable the independent timer before transfer; it currently has no Texas source to
run. A previous `source-handoff.json` with outcome `pending` requires inspection
before another transfer, rather than blindly overwriting a partial handoff.

```bash
sudo systemctl enable --now election-expansion.timer
sudo "$texasRelease/.venv/bin/python" "$texasRelease/scripts/deploy.py" transfer-source --source tx_2026_general --release "$texasRelease" --image "election-pipeline:$texasCommit" --status unofficial --evidence-file /tmp/texas-civix-probe.txt --validation-report /tmp/texas-civix-validation.json
```

Expect `outcome=transferred`. This deliberately removes only Texas from the old
worker's `PIPELINE_SOURCES`, records its new immutable image in the catalog and
preserves public coverage. It leaves IMAGE_TAG, passwords, other source entries,
database containers and existing publications alone. The validation evidence is
saved under the existing data/accuracy tree so backups include it. Exceptions
during the two-file handoff restore the original selection/catalog. A host crash
between writes can leave the handoff pending and needs inspection of that journal
and the private production.env backup before resuming.

Start the independent worker and inspect its result:

```bash
sudo systemctl start election-expansion.service
sudo journalctl -u election-expansion.service -n 40 --no-pager -o cat
curl -fsS http://127.0.0.1/api/states/TX
```

The new worker downloads a fresh response, parses, loads, runs the normal dbt/data
checks and publishes atomically. Failure retains the previous publication. Its
next five-minute runs remain independent of the other four baseline states.

Verify the Texas API shows `publication.status=unofficial`, the new Civix
`publication.source_url`, a fresh publication and numeric candidate totals. The
browser continues its normal polling; it does not need a restart to see new votes.
The source-page hyperlink changes when a dashboard image with this release is
activated using the already-tested private-preview/Caddy handoff workflow.

Finish with a backup and monitoring check from the existing control release:

```bash
sudo systemctl start election-backup.service
sudo cat /srv/election-pipeline/backups/latest-backup.json
sudo systemctl start election-monitor.service
sudo cat /var/lib/election-pipeline-monitor/latest-check.json
```

The backup should be uploaded and include both the frozen baseline release and
Texas's replacement release. Monitoring may still report unrelated source errors;
inspect the source-specific issue keys rather than treating the whole deployment
as healthy solely because Texas now publishes.
