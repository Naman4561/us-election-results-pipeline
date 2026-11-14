#!/usr/bin/env bash
# Ship one approved revision: wait for its CI, then stage and release it between worker
# runs. Fetch this script from GitHub at the revision itself, as for stage_release.sh,
# and run it detached so a closed session cannot stop it halfway:
#
#   curl -fsSL -o /tmp/ship.sh https://raw.githubusercontent.com/Naman4561/us-election-results-pipeline/REVISION/scripts/ship_release.sh
#   sudo systemd-run --unit=election-ship-SHORT --collect bash /tmp/ship.sh refs/heads/BRANCH REVISION
#
# It gives up without changing anything if CI fails, stays queued (a GitHub outage),
# or does not finish in time, or if the branch has moved past the approved revision.
# The worker keeps running throughout: release checks reuse unchanged sources, so a ship
# takes about a minute, and a worker run in progress finishes on the image it started on.
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this script with sudo.\n' >&2
    exit 1
fi
ref="${1:-}"
revision="${2:-}"
if [[ ! "$ref" =~ ^refs/heads/[A-Za-z0-9_./-]+$ || ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Usage: ship_release.sh refs/heads/BRANCH FULL_40_CHARACTER_REVISION\n' >&2
    exit 1
fi
repository='https://github.com/Naman4561/us-election-results-pipeline'
api='https://api.github.com/repos/Naman4561/us-election-results-pipeline'
ci_seconds="${SHIP_CI_SECONDS:-21600}"
queued_seconds="${SHIP_QUEUED_SECONDS:-1800}"
now() { TZ=America/New_York date '+%H:%M:%S %Z'; }

# GitHub's unauthenticated API allows 60 requests an hour: poll every two minutes.
started="$(date +%s)"
while true; do
    verdict="$(python3 - "$api" "$revision" "$queued_seconds" <<'PY' || echo "waiting api-error"
import json, sys, urllib.error, urllib.request
from datetime import datetime, timezone

api, revision, queued = sys.argv[1], sys.argv[2], int(sys.argv[3])
# Production runs Linux: the Windows suite still runs in CI but does not hold a release.
required = {"test (ubuntu-latest)", "dashboard", "postgres-1", "postgres-2"}


def get(path):
    request = urllib.request.Request(
        api + path, headers={"Accept": "application/vnd.github+json", "User-Agent": "election-ship"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


try:
    runs = get(f"/commits/{revision}/check-runs?per_page=100")["check_runs"]
except urllib.error.HTTPError as error:
    if error.code != 422:
        raise
    sys.exit(print("failed unknown-revision"))  # GitHub has no such commit
latest = {}
for run in sorted(runs, key=lambda r: r["id"]):
    latest[run["name"]] = run  # a re-run supersedes the earlier attempt
states = {n: (latest[n]["status"], latest[n]["conclusion"]) if n in latest else None for n in required}
summary = " ".join(f"{n.split()[-1]}={s[1] if s else 'none'}" for n, s in sorted(states.items()))
if any(s and s[0] == "completed" and s[1] != "success" for s in states.values()):
    print("failed", summary)
elif all(s == ("completed", "success") for s in states.values()):
    print("passed", summary)
else:
    # A run that never starts (a GitHub outage left one queued for hours) is not waited out.
    now = datetime.now(timezone.utc)
    stuck = [
        r for r in get(f"/actions/runs?head_sha={revision}")["workflow_runs"]
        if r["status"] == "queued"
        and (now - datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).total_seconds() > queued
    ]
    print("stuck" if stuck and not latest else "waiting", summary)
PY
)"
    case "$verdict" in
        passed*) printf '== CI passed %s: %s\n' "$(now)" "${verdict#passed }"; break ;;
        failed*) printf '== CI failed %s: %s; not shipping\n' "$(now)" "${verdict#failed }"; exit 1 ;;
        stuck*)
            printf '== CI is stuck in the queue %s; not shipping. Cancel it and run CI again.\n' "$(now)"
            exit 1 ;;
    esac
    if (( $(date +%s) - started >= ci_seconds )); then
        printf '== CI did not finish within %s seconds %s; not shipping\n' "$ci_seconds" "$(now)"
        exit 1
    fi
    sleep 120
done

# stage_release.sh stages the branch's head: it must still be the approved revision.
head="$(git ls-remote --exit-code "$repository.git" "$ref" | awk '{print $1}')"
if [[ "$head" != "$revision" ]]; then
    printf '== %s moved to %s; not shipping %s\n' "$ref" "$head" "$revision"
    exit 1
fi

printf '== staging %s\n' "$(now)"
stage="/tmp/stage_release_$revision.sh"
log="/tmp/stage_$revision.log"
curl -fsSL -o "$stage" "https://raw.githubusercontent.com/Naman4561/us-election-results-pipeline/$revision/scripts/stage_release.sh"
if ! bash "$stage" "$ref" >"$log" 2>&1; then
    tail -40 "$log"
    exit 1
fi
grep -E 'CI passed|modules and|^Ran |^OK|Release staged' "$log" | tail -8

release="/opt/election-releases/$revision"
while [[ "$(systemctl show -p ActiveState --value election-backup.service)" == activating ]]; do
    sleep 30
done
printf '== releasing %s\n' "$(now)"
"$release/.venv/bin/python" "$release/scripts/deploy.py" release --release "$release" \
    --image "election-pipeline:$revision" | python3 -c '
import json, sys
result = json.load(sys.stdin)
keys = ("outcome", "error", "migrations_applied", "sources_validated", "sources_reused")
print(json.dumps({k: result.get(k) for k in keys}))
sys.exit(result.get("outcome") != "released")'
if [[ "$(readlink -f /opt/election-releases/current)" != "$release" ]]; then
    printf '== current is not %s after the release\n' "$release"
    exit 1
fi
printf '== released %s %s\n' "$revision" "$(now)"
