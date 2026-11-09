# Deployment

Production runs one **release** at a time: an immutable checkout of one commit under
`/opt/election-releases/<revision>`, with its own control venv and the Docker image built
from it. The systemd units run from the symlink `/opt/election-releases/current`; every
job and dashboard runs in the release image. The catalog
(`/var/lib/election-pipeline-deployment/catalog.json`) records the current and previous
release, the public dashboard, and each source's flags:

| Flag | Meaning |
| --- | --- |
| `enabled` | The worker collects it (HTTP) or imports are accepted (manual) |
| `public` | The public dashboard shows it |
| `status` | `test` or `unofficial`, carried into every publication |
| `acquisition` | `http`, polled by the worker, or `manual`, imported by an operator |

Commands below run as root from the release's venv. With `R` the release directory:

```bash
deploy() { sudo "$R/.venv/bin/python" "$R/scripts/deploy.py" "$@"; }
deploy status
```

## Releasing

1. **Stage** the pushed branch. Nothing running changes; the tests run again inside the
   new image, with no network or production mounts.

   ```bash
   sudo bash scripts/stage_release.sh refs/heads/main
   ```

2. **Release** it. Migrations are applied (they are additive), then every enabled HTTP
   source runs once under the new image in check-only mode: collect, parse, load and
   the full dbt build, with the candidate discarded and nothing published. Only when
   every source validates does `current` move to the release; the worker uses it from
   its next cycle. A source that fails leaves the old release current.

   ```bash
   deploy release --release "$R" --image "election-pipeline:<revision>"
   ```

3. **Move the dashboard** to the release when its code changed:
   `deploy start-dashboard`, then `deploy activate --candidate <id>` (below).

`deploy rollback-release` returns the worker to the previous release. Migrations stay
applied; a release never depends on a migration being absent.

## Adding a state in production

The state's source is in the release's `sources.toml` and starts disabled and private.
Its first publication is checked before it becomes public.

```bash
deploy prepare --source xx_2026_general        # collect, parse, load, dbt; no publish
deploy promote --source xx_2026_general        # first publication, still private
sudo systemctl start election-backup.service    # the uploaded backup the check restores
sudo "$R/.venv/bin/python" "$R/scripts/production_recovery.py" check-restore \
    --backup-id <backup_id from /srv/election-pipeline/backups/latest-backup.json>
deploy start-dashboard --source xx_2026_general  # private candidate on a local port
deploy activate --candidate <id> --restore-report <recovery_checks/....json>
```

Check the candidate dashboard's totals against the state's own site before
activating. Activation requires a passed restore check from the last 30 minutes that
verified the new publication. Caddy is switched with a journal, and the switch is
undone if the public check fails; `deploy recover-handoff` finishes an interrupted one.
The state is then enabled and public.

To withdraw it, `deploy rollback-dashboard` restores the previous dashboard and source
flags. `deploy disable --source` stops collecting a private source; a public one must
first leave through a dashboard switch. `deploy discard-dashboard --name` removes an
unused candidate.

Manual sources and Mississippi's reviewed counties have their own procedures:
[manual results](manual-results.md) and [Mississippi collection](mississippi-collection.md).

## Settings

`/etc/election-pipeline/deployment.env` (from `deploy/deployment.env.example`) holds
paths and resource budgets, never passwords:

| Setting | Default | Meaning |
| --- | --- | --- |
| `JOB_CPUS`, `JOB_MEMORY` | `0.5`, `768m` | Limits of every job and dashboard container |
| `JOB_PARALLEL` | `1` | Jobs at once (1–4), each with those limits |
| `JOB_TIMEOUT` | `240` | Seconds before one job is stopped |
| `CYCLE_SECONDS` | `270` | One worker cycle's budget; the timer fires every 5 minutes |

Raise `JOB_PARALLEL` only after measuring headroom: PostgreSQL and the dashboard share
the host. Backups pause collection for a few seconds only; see [backups](backups.md).

## Moving a host from per-source pins (one time)

A host deployed before release 0.2.0 pinned an image per source and ran two
collectors. `scripts/cutover.py` moves it to one release:

```bash
sudo systemctl start election-backup.service
sudo "$R/.venv/bin/python" "$R/scripts/cutover.py" --release "$R" --image "election-pipeline:<revision>"
sudo "$R/.venv/bin/python" "$R/scripts/cutover.py" --release "$R" --image "election-pipeline:<revision>" --apply
```

The first run prints the plan. `--apply` refuses without an uploaded backup from the
last hour, then stops both collectors and waits for running jobs and applies migration
024. It runs every enabled HTTP source once, check-only, in the new image; if any fails,
the collectors restart, nothing else has changed, and the pinned images keep running.
Otherwise it copies every per-state row into the shared tables (each table's counts and vote sums
verified before commit), writes the new catalog and settings, points `current` at the
release, installs the units and moves the public site to a dashboard from the release.
To undo it, restore that backup.
