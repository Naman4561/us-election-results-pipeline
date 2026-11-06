# Operational hardening (November 2026 review)

Changes from the repository review, as separate commits on top of the nine-state
release. Each is covered by tests; the full suite, dashboard test, ruff and prettier
pass.

| Review item | Change |
| --- | --- |
| Failing sources retried every 5 minutes | Persistent backoff: 5, 15, 45 minutes, then hourly; access refusals (401/403/451) up to 6 hours. No request and no record while backing off; a forced download still goes out; success clears it. The monitor explains three or more consecutive failures or any refusal. |
| Anonymous user agent | `US-Election-Results-Pipeline/0.1 (+https://github.com/Naman4561/us-election-results-pipeline)`. Re-pins may change only this setting. |
| Unbounded growth | "Not due" checks write no fetch record. Loads skip fetch records already finished in the database, unread. `scripts/prune_data.py` (dry run by default) removes old run reports, failed parse/load reports, dbt artifacts and non-current parsed outputs; it never touches raw archives, fetch metadata, quarantine or state. |
| Identical republishing | The scheduled runner keeps the live publication when a new snapshot's parsed outputs are byte-identical to it. |
| Dashboard load | `/api/national` omits per-county detail (nine new states: 1.6 MB to 97 KB raw); the page loads `/api/states/{code}` when a state is opened. Each reader reuses its payload for 10 seconds. Static files revalidate by ETag (`Cache-Control: no-cache`) instead of `?v=expansion2`. |
| Redirects validated late | Every redirect hop is checked before it is requested. |
| Parser crashes | Any parser exception becomes `validation_failed` with its type. Iowa and Maryland checks are explicit. Maryland accepts a collection that straddles a page refresh and records every contest's time. |
| California/Maryland polling | New `gated_files` collector: one request while the primary file is unchanged (refreshed at least hourly). |
| Duplicated collector code | `collectors/common.py` (`Bundle`) is shared by all bundle collectors. |
| Repository hygiene | Root patch files and the stray diff capture removed; root `*.patch` ignored. |
| Rosters in Python | `resources/rosters_2026/<STATE>.json` with provenance notes; included in the parser code hash. `scripts/review_rosters.py` proposes a roster from a captured snapshot. |
| `or.py` | Oregon's parser is `parsers/oregon.py` (`State.parser_module`); its schema stays `or`. |
| Reproducible builds | `constraints.txt` pins all Python dependencies; the build base image is pinned by digest; Dependabot proposes updates; CI checks prettier. |

## Deliberately not changed

- **Production database and Caddy images** (`postgres:17`, `caddy:2` in
  `compose.production.yml`). Pinning them could make Compose recreate the live database
  container on another PostgreSQL minor version at the next deploy. To pin them to what
  is running now, first record the running digests (read-only):

  ```bash
  sudo docker inspect --format '{{.Config.Image}} {{index .RepoDigests 0}}' $(sudo docker ps --format '{{.Names}}' | grep -E 'db|caddy')
  ```

  then pin those exact digests in a separate, reviewed change.
- **Tests and dev tools in the image.** `scripts/stage_coverage_release.sh` runs the full
  test suite inside the built image as a release gate, so they are needed there.
- **Worker capacity.** Whether one sequential worker keeps up needs production run
  durations; see below.
- **Stricter lint rules.** The `flake8-bugbear` loop-variable findings are all test
  closures called within the same iteration; enabling the rules would add churn only.

## Roll out

No command below has been executed by the assistant. These commits apply on top of the
nine-state commit. If the nine states are not deployed yet, stage this combined branch
and follow `docs/nine-state-expansion.md`; new sources get every change at once.

Existing expansion sources keep their pinned images until re-pinned. To give one the
backoff, storage and collector fixes (each re-pin runs a validated prepare-only cycle
first and changes nothing if it fails):

```bash
sudo "$CR/.venv/bin/python" "$CR/scripts/deploy.py" repin --release "$CR" --image "election-pipeline:$REV" --source ky_2026_general
```

The dashboard changes need a new dashboard candidate (`start-dashboard` and `activate`
as in the nine-state notes). The baseline sources run by `election-pipeline.service`
change only when `IMAGE_TAG` in `/etc/election-pipeline/production.env` is updated,
which is outside this release.

### Retention

Plan first; nothing is deleted without `--apply`:

```bash
sudo docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges --mount type=bind,src=/srv/election-pipeline/data,dst=/app/data "election-pipeline:$REV" python scripts/prune_data.py --keep-days 7
```

Review the planned paths, then rerun with `--apply`. It takes each source's pipeline
lock, so it is safe while the worker runs; locked sources are skipped and reported.

### Capacity check (read-only)

To decide whether the single worker needs help, send the outcome and per-source
durations from the latest cycle (the worker redacts passwords from this file; `CONTROL_STATE_DIR` in `/etc/election-pipeline/deployment.env` names its folder if it is not the default):

```bash
sudo python3 -c 'import json; d=json.load(open("/var/lib/election-pipeline-deployment/expansion-latest.json")); print(d["finished_at"], d["outcome"]); [print(r.get("source_id"), r.get("outcome"), r.get("duration_seconds")) for r in d["results"]]'
```

Many `deferred: Cycle budget reached` results mean sources are waiting more than one
cycle; the options then are a second worker or reusing dbt's parsed project per image.
