#!/usr/bin/env bash
# Stage a release: an immutable checkout, its control venv and its tested image.
# Nothing running changes; `deploy.py release` makes it current afterwards.
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this script with sudo bash.\n' >&2
    exit 1
fi

repository='https://github.com/Naman4561/us-election-results-pipeline.git'
requested_ref="${1:-refs/heads/main}"
if [[ ! "$requested_ref" =~ ^refs/heads/[A-Za-z0-9_./-]+$ ]]; then
    printf 'Supply a complete branch ref, for example refs/heads/main.\n' >&2
    exit 1
fi
revision="$(git ls-remote --exit-code "$repository" "$requested_ref" | awk '{print $1}')"
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'The requested branch was not found on GitHub.\n' >&2
    exit 1
fi

# The tests run in GitHub Actions on every push (.github/workflows/ci.yml): the full suite
# on Linux and Windows, the dashboard's JavaScript and the PostgreSQL tests in two shards.
# A revision is staged only once the jobs production depends on passed for it (Windows
# runs for local development and does not hold a release), so this host, whose CPU also
# collects and serves results, does not run them again. STAGE_FULL_TESTS=1 runs the suite
# here too. ship_release.sh waits for the same jobs (tests/test_check_pipeline.py).
python3 - "$revision" <<'PY'
import json, sys, urllib.request

revision = sys.argv[1]
required = {"test (ubuntu-latest)", "dashboard", "postgres-1", "postgres-2"}
url = (
    "https://api.github.com/repos/Naman4561/us-election-results-pipeline/commits/"
    f"{revision}/check-runs?per_page=100"
)
request = urllib.request.Request(
    url, headers={"Accept": "application/vnd.github+json", "User-Agent": "election-stage"}
)
with urllib.request.urlopen(request, timeout=30) as response:
    runs = json.load(response)["check_runs"]
latest = {}
for run in sorted(runs, key=lambda r: r["id"]):
    latest[run["name"]] = run  # a re-run supersedes the earlier attempt
problems = sorted(
    f"{name}: {latest[name]['status']}/{latest[name]['conclusion']}"
    if name in latest
    else f"{name}: not run"
    for name in required
    if name not in latest
    or (latest[name]["status"], latest[name]["conclusion"]) != ("completed", "success")
)
if problems:
    sys.exit(f"CI has not passed for {revision}: " + "; ".join(problems))
print("CI passed: " + ", ".join(sorted(required)))
PY

release="/opt/election-releases/$revision"
if [[ ! -d "$release/.git" ]]; then
    git clone --no-checkout "$repository" "$release"
    git -C "$release" checkout --detach "$revision"
fi
if [[ "$(git -C "$release" rev-parse HEAD)" != "$revision" || -n "$(git -C "$release" status --porcelain --untracked-files=all)" ]]; then
    printf 'The release checkout has a different commit or local changes. Stop here.\n' >&2
    exit 1
fi

# The host control scripts' dependencies (the postgres and deployment extras, pinned by
# the lock) live in one environment per lock under /opt/election-venvs, shared by every
# release with that lock, so a code-only release installs nothing. The release's own venv
# adds that environment and the release's src/ to its path. The first line printed is the
# environment's key; the rest are the requirements.
mapfile -t host < <(python3 - "$release" <<'PY'
import hashlib, sys, tomllib
from pathlib import Path
root = Path(sys.argv[1])
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
extras = project["optional-dependencies"]
wanted = [*project["dependencies"], *extras["postgres"], *extras["deployment"]]
digest = hashlib.sha256((root / "constraints.txt").read_bytes() + "\n".join(wanted).encode())
print(digest.hexdigest()[:16], *wanted, sep="\n")
PY
)
if [[ ! "${host[0]:-}" =~ ^[0-9a-f]{16}$ || "${#host[@]}" -lt 2 ]]; then
    printf 'Could not read the host requirements from pyproject.toml. Stop here.\n' >&2
    exit 1
fi
shared="/opt/election-venvs/${host[0]}"
if [[ ! -x "$shared/bin/python" ]]; then
    mkdir -p /opt/election-venvs
    building="$(mktemp -d /opt/election-venvs/.building-XXXXXX)"
    python3 -m venv "$building"
    PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 "$building/bin/python" -m pip install \
        -c "$release/constraints.txt" "${host[@]:1}"
    mv "$building" "$shared"
fi
purelib='import sysconfig; print(sysconfig.get_paths()["purelib"])'
python3 -m venv --without-pip "$release/.venv"
packages="$("$release/.venv/bin/python" -c "$purelib")"
printf 'import site; site.addsitedir("%s")\n' "$("$shared/bin/python" -c "$purelib")" \
    > "$packages/election-dependencies.pth"
printf '%s\n' "$release/src" > "$packages/election-pipeline.pth"
"$release/.venv/bin/python" -c 'import dotenv, psycopg, pglast, election_pipeline'

docker build --build-arg "VCS_REF=$revision" --tag "election-pipeline:$revision" "$release"

# A smoke test of the image itself, with no production mounts, credentials or network:
# every module imports with the image's dependencies, the source configuration loads,
# and a manual import and the dashboard run end to end.
isolated=(docker run --rm --network none --cpus 0.5 --memory 768m --memory-swap 768m --cap-drop ALL --security-opt no-new-privileges "election-pipeline:$revision")
"${isolated[@]}" python -c '
import importlib, pkgutil
from pathlib import Path
import election_pipeline
from election_pipeline.settings import load_config
names = [m.name for m in pkgutil.walk_packages(election_pipeline.__path__, "election_pipeline.") if not m.name.endswith("__main__")]
for name in names:
    importlib.import_module(name)
print(len(names), "modules and", len(load_config(Path("config/sources.toml"))[1]), "sources load")
'
for tests in test_oklahoma.py test_dashboard.py; do
    "${isolated[@]}" python -m unittest discover -s tests -p "$tests"
done
if [[ "${STAGE_FULL_TESTS:-0}" == 1 ]]; then
    "${isolated[@]}" python -m unittest discover -s tests -v
fi

printf '\nRelease staged: %s\nMake it current with:\n  sudo %s/.venv/bin/python %s/scripts/deploy.py release --release %s --image election-pipeline:%s\n' \
    "$release" "$release" "$release" "$release" "$revision"
