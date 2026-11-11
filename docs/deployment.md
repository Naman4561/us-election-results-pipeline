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
| `status` | `test` or `unofficial`, carried into every publication; `deploy mark-unofficial --source NAME` moves a source from `test` to `unofficial`, and the worker's next run republishes it so labelled |
| `acquisition` | `http`, polled by the worker, or `manual`, imported by an operator |

Commands below run as root from the release's venv. With `R` the release directory:

```bash
deploy() { sudo "$R/.venv/bin/python" "$R/scripts/deploy.py" "$@"; }
deploy status
```

## Releasing

1. **Stage** the pushed branch. Nothing running changes. Staging starts only once every
   GitHub Actions job (`ci.yml`: the suite on Linux and Windows, the dashboard and
   PostgreSQL) has passed for that commit, so this host does not run the suite again;
   inside the new image, with no network or production mounts, it imports every module,
   loads the source configuration and runs two quick end-to-end test files
   (`STAGE_FULL_TESTS=1` runs the whole suite as well). The image installs the locked
   dependencies (`constraints.txt`) in a layer of its own, before any code, so a release
   that changes only code reuses it and adds a few megabytes. The host control scripts'
   dependencies live in one environment per lock under `/opt/election-venvs`, which the
   release's venv points to, so a code-only release installs nothing on the host.

   ```bash
   sudo bash scripts/stage_release.sh refs/heads/main
   ```

2. **Release** it. Migrations are applied (they are additive), then every enabled HTTP
   source runs once under the new image in check-only mode: its latest archived
   snapshot is parsed, loaded and put through the full dbt build, with the candidate
   discarded and nothing published. No request goes to the state and collector state is
   untouched, so a state's site being down does not block a release. A source the
   worker is collecting at that moment is locked; its check waits for it, retrying every
   15 seconds for up to `CHECK_LOCK_WAIT_SECONDS` (default 600). Only when every source
   validates does `current` move to the release; the worker uses it from its next cycle.
   A source that fails leaves the old release current.

   A check that passed is not repeated while nothing it depends on changes. Each
   source's check key (`control.check_keys`) covers the release files its check can
   run: the image's build inputs, dbt models, migrations, the job scripts, the package
   modules reachable from them and from the source's own parser and collector, and the
   reviewed data other than other states'. It also covers the source's configuration,
   its status and the snapshot it would check. Passes are recorded by key in
   `release-checks.json` in the control state directory, kept even when a release fails,
   so a retry checks only what failed. A change to one state's parser or data re-checks
   that state; a change to shared parsing code, `states.py`, dbt, migrations or
   dependencies re-checks every source; dashboard and host-side changes re-check none.
   `deploy.py release --check-all` checks every source regardless.

   Once `current` has moved, old artifacts are pruned: release checkouts and images
   other than the current and previous releases, the two newest others and any image a
   container was created from (the active and previous dashboards), host environments no
   kept release uses, and the Docker build cache beyond 4 GB. The result reports what
   was removed; a pruning error is reported without undoing the release.

   ```bash
   deploy release --release "$R" --image "election-pipeline:<revision>"
   ```

   A release that moves a source to a new address has no snapshot of that address to
   check. Name it with `--moved-source <source_id>`: having no snapshot then passes for
   that source alone, and its first collection is validated before it publishes.

3. **Move the dashboard** to the release when its code changed:
   `deploy start-dashboard`, then `deploy activate --candidate <id>` (below).

`deploy rollback-release` returns the worker to the previous release. Migrations stay
applied; a release never depends on a migration being absent.

## Adding a state in production

The state's source is in the release's `sources.toml` and starts disabled and private.
Its first publication is checked before it becomes public.

```bash
deploy register --source xx_2026_general       # a state new since cutover: add it, disabled
deploy prepare --source xx_2026_general        # collect, parse, load, dbt; no publish
deploy promote --source xx_2026_general        # first publication, still private
sudo systemctl start election-backup.service    # the uploaded backup the check restores
sudo "$R/.venv/bin/python" "$R/scripts/production_recovery.py" check-restore \
    --backup-id <backup_id from /srv/election-pipeline/backups/latest-backup.json>
deploy start-dashboard --source xx_2026_general  # private candidate on a local port
deploy activate --candidate <id> --restore-report <recovery_checks/....json>
```

Check the candidate dashboard's totals against the state's own site before
activating. Activation requires a passed restore check from the last 24 hours that
verified the new publication, finished after it was published.
`election-check-restore.timer` fires hourly at :40 and runs a check
(`check-restore --if-awaiting`, on the newest uploaded backup) only when a promoted state
that is not yet public is in the latest backup and no passed check has verified it; every
other hour it exits at once. A state promoted between backups is thus verified within the
hour after the next six-hourly backup, with no backup of its own, and states promoted
together share one check. `check-restore --backup-id ID` still runs one on demand. Caddy is switched with a journal, and the switch is
undone if the public check fails; `deploy recover-handoff` finishes an interrupted one.
The state is then enabled and public.

To withdraw it, `deploy rollback-dashboard` restores the previous dashboard and source
flags. `deploy disable --source` stops collecting a private source; a public one must
first leave through a dashboard switch. `deploy discard-dashboard --name` removes an
unused candidate.

Only the active and previous dashboards are kept running. `start-dashboard` removes
older candidates before starting its own, so at most three run while one is checked,
and `activate` removes every dashboard but the new active one and the one it replaced.

`deploy pause --source` stops collecting a source while its last publication stays
public, such as a site that is down for days; `deploy resume --source` starts it again.
The worker records a paused source as `paused`, and the monitor shows a reminder in
place of its collection checks.

## Certified results

Where a state's own feed marks its results official, the parser records it and the
monitor shows a reminder:

| States | What marks them official |
| --- | --- |
| Georgia, Washington, Idaho, Virginia | Enhanced Voting's `isOfficialResults` |
| Colorado, Iowa, West Virginia, South Carolina | Clarity's final-upload flag |
| South Dakota, New Mexico, Nebraska, Oregon, Maryland, Alaska | The results page's own "Unofficial" label reading "Official" |
| Texas | The feed offering its official canvass report (`OfficialCanvassReport`), as it does for canvassed statewide elections |

The page labels' official wording has not been seen on these sites this election. A
page whose label reads neither "Unofficial" nor "Official" (for example "Certified"),
or pages that disagree, are refused: the monitor reports the failed parse, the last
publication stays up, and the new wording is added after it is checked. South Dakota's
and New Mexico's county workbooks must carry the same label as the page.

Check the totals against the state's certified results, then:

```bash
deploy certify --source ga_2026_general
```

It pauses the source, publishes its current snapshot with status `official` (refused
unless the snapshot is marked official), and leaves it paused for good with status
`official`; the dashboard labels it "Official results". A refused certification
restores the pause as it was.

### Operator-attested certification

Where the feed has no official marker (North Carolina, Minnesota, Hawaii, Florida), the
operator attests to the state's certification. Both modes record the attestation with
the publication (`serving.publications.attestation`): who attests, the state's
certification date and its own certification notice or file.

**Approve the published snapshot** when the state certifies the same numbers its
feed already shows. Compare the public dashboard with the certified results, then
approve the snapshot it shows by its raw SHA-256 (the `raw_sha256` of the source's
latest run report). Any other snapshot is refused, so a collection between the check
and the approval cannot slip through.

```bash
deploy certify --source nc_2026_general_all --approve-snapshot RAW_SHA256 \
    --certified-on 2026-11-24 --attested-by "NAME" \
    --evidence-url https://www.ncsbe.gov/...
```

**Import the certified file** when the state publishes its certified results apart
from its feed. Place the file under the data inbox, as for [manual
results](manual-results.md), and name its format. A state's own certified format is
read by that state's parser (`CERTIFIED` in its module); `certified-results.csv`, the
default, is the operator's transcription of a certificate published only as a document
(see `parsers/certified.py`), checked against the state's races, roster and counties,
with county rows adding up to the statewide totals.

```bash
deploy certify --source ky_2026_general --certified-file INBOX_FILE \
    --certified-name certified-results.csv --sha256 HASH --downloaded-at UTC_TIME \
    --certified-on 2026-11-23 --attested-by "NAME" --evidence-url https://elect.ky.gov/...
```

The file joins the source's raw archive as a snapshot holding only it. Releases check
an official source's newest collected snapshot as unofficial; a check publishes
nothing.

Manual sources and Mississippi's reviewed counties have their own procedures:
[manual results](manual-results.md) and [Mississippi collection](mississippi-collection.md).

## Settings

`/etc/election-pipeline/deployment.env` (from `deploy/deployment.env.example`) holds
paths and resource budgets, never passwords:

| Setting | Default | Meaning |
| --- | --- | --- |
| `JOB_CPUS`, `JOB_MEMORY` | `0.5`, `768m` | Limits of every job and dashboard container |
| `DASHBOARD_CPUS` | `JOB_CPUS` | A dashboard container's CPU budget, if it differs |
| `DASHBOARD_READY_SECONDS` | `180` | How long a new dashboard may take to read every state |
| `JOB_PARALLEL` | `1` | Jobs at once (1–4), each with those limits |
| `JOB_TIMEOUT` | `240` | Seconds before one job is stopped |
| `CYCLE_SECONDS` | `600` | One worker cycle's budget. The timer fires every 15 minutes (the host's CPU is burstable, with a 20% baseline) and skips ticks while a cycle runs; normal cycles take 1–2 minutes, the one after a release that changes a state's publication, longer |

A source that is not due for collection, and whose last run published the snapshot it
holds under the current image and status, is skipped without starting a container (its
result reads `not_due` with `skipped: idle`). It still runs at least every 30 minutes,
so its run report stays fresh for the monitor. `worker-idle.json` in the control state
directory records what each source's last good run published. A release republishes
only the states whose own registry entry, dbt models or publication code changed
(`publication.project_hash`), not every state when one is added.

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
024. It checks every enabled HTTP source's latest snapshot in the new image (as a
release does); if any fails,
the collectors restart, nothing else has changed, and the pinned images keep running.
Otherwise it copies every per-state row into the shared tables (each table's counts and vote sums
verified before commit), writes the new catalog and settings, points `current` at the
release, installs the units and moves the public site to a dashboard from the release.
To undo it, restore that backup.
