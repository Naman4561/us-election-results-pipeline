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

# Run the Linux-only controls tests with no production mounts, credentials or network.
docker run --rm --network none --cpus 0.5 --memory 768m --memory-swap 768m --cap-drop ALL --security-opt no-new-privileges "election-pipeline:$revision" python -m unittest discover -s tests -v

printf '\nRelease staged: %s\nMake it current with:\n  sudo %s/.venv/bin/python %s/scripts/deploy.py release --release %s --image election-pipeline:%s\n' \
    "$release" "$release" "$release" "$release" "$revision"
