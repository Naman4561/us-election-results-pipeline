# Adding states while existing collectors keep running

The accepted baseline is commit `b8c441896a1f280b8a876f9953f5f0121364c629` with NC,
KY, MN, NE and TX. Its collector timer, service, image tag, database container and
volume remain in place. This change does not add another state or change any
publication from `test` to `unofficial`.

## Design decision

Use immutable Git checkouts and Docker image IDs for new states. Keep one separate,
bounded expansion worker, and start a second dashboard privately before switching
the existing Caddy process to it. Keep the preceding dashboard running for rollback.
Docker Compose in another directory would still select `election-production`
because our production Compose file explicitly names that project.

| Component | Ownership |
| --- | --- |
| Existing five-state collector | Existing checkout, image and systemd unit |
| New-state collector | Its registered image ID; expansion worker selects it alone |
| Data | Existing `/srv/election-pipeline/data`, namespaced by source/state |
| Database | Existing PostgreSQL, separate state schemas and publication locks |
| Deployment catalog | Root-owned JSON, no credentials, readable by dashboard |
| Backups and monitoring | Versioned control checkout; catalog includes registered states |
| Dashboard | Separate reader-only container with an immutable approved coverage file |

The host still shares CPU, RAM, disk and PostgreSQL. Resource caps and short migration
timeouts reduce interference; this is not complete infrastructure isolation. Measure
headroom before running an additional worker and dashboard. Build images on the laptop
or CI, since a build on the Lightsail host competes with live reporting.

## Apply and check on Windows first

Apply the incremental patch in a clean feature branch based on the accepted commit.
`git apply --check` must pass before `git apply`. Existing work should be committed
or kept on its own branch first.

```powershell
git switch -c feature/isolated-deployments
$patchPath = "$env:USERPROFILE\Downloads\isolated-deployments.patch"
git apply --check $patchPath
git apply $patchPath
.\.venv\Scripts\python -m pip install -e ".[postgres,transform,dashboard,deployment]"
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python scripts/check_pipeline.py --pattern test_deployment.py
npm test
git add .
git commit -m "feat: isolate state expansion workers and dashboard deployments"
git push -u origin feature/isolated-deployments
```

The native deployment test creates and drops its own temporary database. It tests
that a blocked foreign key migration times out, rolls back its new schema and
leaves existing results intact, then succeeds after the lock is released. Linux
filesystem lock/unit-install checks run in CI; their Windows skips are expected.

Record the new commit with `git rev-parse HEAD`. Keep the production checkout on
its accepted commit.

## Install the controls in a new Ubuntu checkout

Set `releaseCommit` to the newly pushed full commit, not the old baseline. Each
line below is a complete command; no shell continuation or heredoc is needed.

```bash
releaseCommit=REPLACE_WITH_NEW_FULL_COMMIT
controlRelease="/opt/election-releases/$releaseCommit"
sudo mkdir -p /opt/election-releases
sudo git clone --no-checkout https://github.com/Naman4561/us-election-results-pipeline.git "$controlRelease"
sudo git -C "$controlRelease" checkout --detach "$releaseCommit"
sudo python3 -m venv "$controlRelease/.venv"
sudo "$controlRelease/.venv/bin/python" -m pip install --upgrade pip
sudo "$controlRelease/.venv/bin/python" -m pip install -e "$controlRelease[postgres,transform,dashboard,deployment]"
free -h
sudo docker stats --no-stream
sudo cp "$controlRelease/deploy/deployment.env.example" /etc/election-pipeline/deployment.env
sudo nano /etc/election-pipeline/deployment.env
```

Set `CONTROL_ROOT` to the new checkout, `CADDY_CONTAINER` to the existing Caddy ID,
and choose CPU/RAM limits with room for both a candidate dashboard and an expansion
job alongside the existing services. The example limits are placeholders, not a
capacity estimate. Find Caddy without changing it:

```bash
sudo docker compose --env-file /etc/election-pipeline/production.env -f /opt/election-pipeline/compose.production.yml --profile public ps -q caddy
```

Record the baseline and install the new units/drop-ins:

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" init
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" install-controls
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" status
```

Installation runs `systemctl daemon-reload`, leaving existing jobs running. The
backup and monitor use the versioned controls on their next invocation. It does
not edit `production.env`, retag an image, rerun bootstrap, stop a timer, recreate
PostgreSQL or restart the existing collectors.

Confirm the new monitor reports `healthy` and the next hourly backup is uploaded.
If the backup is already running, let it finish; registration refuses to proceed
while a legacy backup invocation might still be in flight.

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/monitor_production.py" --catalog-file /var/lib/election-pipeline-deployment/catalog.json --dry-run
sudo systemctl show election-backup.service -p ExecStart -p ActiveState
sudo systemctl show election-monitor.service -p ExecStart
sudo systemctl enable --now election-expansion.timer
```

Inspect the dry-run JSON: the historical monitor returns exit code zero even for
`unhealthy`. The expansion timer has no sources to run until activation.

## Check the new dashboard with the existing five states

Build the new image on Windows in the clean committed checkout. This produces a
Docker image transfer. Set `remoteHost`
and `keyPath` to the same values used for the working SSH connection.

```powershell
$releaseCommit = (git rev-parse HEAD).Trim()
$image = "election-pipeline:$releaseCommit"
docker build --platform linux/amd64 --build-arg "VCS_REF=$releaseCommit" -t $image "https://github.com/Naman4561/us-election-results-pipeline.git#$releaseCommit"
$imageArchive = Join-Path $env:TEMP "election-release-$releaseCommit.tar"
docker save -o $imageArchive $image
scp -i $keyPath $imageArchive "${remoteHost}:/tmp/election-release.tar"
```

Never overwrite the old image tag. The Git build context uses the full pushed commit.
This also avoids Windows
working-copy line-ending differences changing the image's source fingerprints.
See [Docker Git build contexts](https://docs.docker.com/build/concepts/context/#git-repositories).

On Ubuntu:

```bash
sudo docker load -i /tmp/election-release.tar
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" start-dashboard --image "election-pipeline:$releaseCommit" --port 8001
curl -fsS http://127.0.0.1:8001/api/national
```

The command returns a candidate `id`. The private dashboard should show the current
five states and published returns. For a browser preview, forward laptop port 8081
to server `127.0.0.1:8001` using the existing SSH method. No firewall change is needed.

After that check, activate the returned candidate ID:

```bash
candidateId=REPLACE_WITH_CANDIDATE_ID
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" activate --candidate "$candidateId"
curl -fsS http://127.0.0.1/api/national
```

This first handoff changes the dashboard backend only. It requires no repeat of the
accepted five-state database restore gate. Future additions require a new-state
restore report. Caddy reloads its config gracefully; the old dashboard stays running.
The existing single-file Caddy bind requires persisting routing changes in place,
with a recovery journal for a host crash during that operation.

## Admit a future state

Develop and test the adapter in a new release. Keep one new-state migration file
per state. It may create the new schema/tables/indexes and grant the reader access
to published tables. The SQL parser rejects live changes to existing tables,
procedural blocks, DML, role changes and grants on raw state tables. Migrations use
the writer login, a 750 ms lock timeout and a 15 second statement timeout. A busy
database causes a retryable failure instead of waiting on existing writers.

Prepare the new image and checkout as above, then register its specific source:

```bash
newRelease=/opt/election-releases/REPLACE_WITH_STATE_RELEASE_COMMIT
newImage=election-pipeline:REPLACE_WITH_STATE_RELEASE_COMMIT
newSource=REPLACE_WITH_NEW_SOURCE_ID
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" register --release "$newRelease" --image "$newImage" --source "$newSource" --status test
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" migrate --source "$newSource" --file "$newRelease/migrations/REPLACE_WITH_NEW_MIGRATION.sql"
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" prepare --source "$newSource"
```

Review the parser/reconciliation output and compare source returns independently
before promoting. Zero or blank pre-reporting data verifies structure, not live vote
accuracy. Choose `unofficial` deliberately when appropriate; the deployment tools
never make that status decision automatically.

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" promote --source "$newSource"
sudo systemctl start election-backup.service
sudo cat /srv/election-pipeline/backups/latest-backup.json
```

Use that uploaded backup ID for the existing S3-download/isolated-restore procedure,
invoked from the control checkout. Run it close to activation: the gate requires a
passed report from the last 30 minutes, created after this state's promotion, with
its exact publication ID, nonempty published table, reader/writer permission checks
and temporary database cleanup. An old five-state-only report cannot satisfy it.

```bash
backupId=REPLACE_WITH_UPLOADED_BACKUP_ID
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/production_recovery.py" check-restore --backup-id "$backupId" --catalog-file /var/lib/election-pipeline-deployment/catalog.json
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" start-dashboard --source "$newSource" --image "$newImage" --port 8002
```

Preview the new candidate. Then use its ID and the restore check's `report_path`:

```bash
candidateId=REPLACE_WITH_NEW_CANDIDATE_ID
restoreReport=/srv/election-pipeline/data/recovery_checks/REPLACE_WITH_RESTORE_REPORT.json
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" activate --candidate "$candidateId" --restore-report "$restoreReport"
sudo systemctl start election-expansion.service
sudo journalctl -u election-expansion.service -n 30 --no-pager -o cat
```

New states run sequentially within a 270 second cycle, with per-job timeouts and
container CPU/RAM limits. A failing state does not prevent another admitted state
from running. States exceeding the cycle budget are reported as deferred, and
monitoring checks each enabled source's freshness separately. Revisit capacity
before adding enough states to exhaust that budget.

## Move an active source to a fixed image

Each registered source is pinned to one image. A code fix for an active source,
such as a collector compatibility change, uses `repin` rather than another
registration. A state may hold only one owner and one replacement.

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" repin \
  --release "$CR" --image "election-pipeline:$REVISION" --source ms_2026_general_all
```

- **What it accepts:** registered expansion sources only. The new image must report
  the identical source contract, collector settings and races, and the release must
  contain every applied migration unchanged.
- **Validation:** the new image runs a prepare-only cycle: collect, parse, load and
  validate a candidate. Nothing is published. The pin changes only if that cycle
  returns `validated`, the catalog entry is unchanged, and no backup is running.
- **What changes:** only `image_id`, `revision`, `release_path` and
  `release_sha256`. The source ID, archives, flags and publications stay the same.
  Earlier pins are kept in `previous_images`.
- **Rollback:** the same command with the previous release and image. Keep the
  previous release directory and image until the new pin has published.

## Recovery and cleanup

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" rollback-dashboard
```

Rollback checks the previous backend, reloads Caddy and disables only the newly
added coverage/worker selections. Existing publications and the original five-state
collectors continue. A new-state job already in flight may finish privately.

After a host crash leaves a `pending` handoff journal, run:

```bash
sudo "$controlRelease/.venv/bin/python" "$controlRelease/scripts/deploy.py" recover-handoff
```

This restores both the preceding Caddy route and its catalog. Monitoring alerts on
a pending journal. Keep active and previous dashboards until the next handoff is
accepted; `discard-dashboard --name NAME` refuses to remove either one. Use unused
candidate ports for later previews, and remove only explicitly discarded candidate
containers. Do not run `docker compose down`, `up --build`, or pull a new commit
into the frozen production checkout during reporting.

Backups cover all registered schemas and sources, even before public activation.
They retain raw evidence, source definitions, each pinned collector release, the
deployment catalog and active/previous dashboard coverage. They do not export
container images on every hourly run: retain or rebuild the registered releases'
images separately, and keep the existing private credentials recovery procedure.
