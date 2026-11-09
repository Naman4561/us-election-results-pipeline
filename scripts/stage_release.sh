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

python3 -m venv "$release/.venv"
PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 "$release/.venv/bin/python" -m pip install -e "${release}[postgres,deployment]"
docker build --build-arg "VCS_REF=$revision" --tag "election-pipeline:$revision" "$release"

# Run the Linux-only controls tests with no production mounts, credentials or network.
docker run --rm --network none --cpus 0.5 --memory 768m --memory-swap 768m --cap-drop ALL --security-opt no-new-privileges "election-pipeline:$revision" python -m unittest discover -s tests -v

printf '\nRelease staged: %s\nMake it current with:\n  sudo %s/.venv/bin/python %s/scripts/deploy.py release --release %s --image election-pipeline:%s\n' \
    "$release" "$release" "$release" "$release" "$revision"
