#!/usr/bin/env bash
# Run from any checkout. Only the immutable checkout and versioned controls are installed.
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run this script with sudo bash.\n' >&2
    exit 1
fi

repository='https://github.com/Naman4561/us-election-results-pipeline.git'
requested_ref="${1:-refs/heads/feat/complete-state-coverage}"
if [[ ! "$requested_ref" =~ ^refs/heads/[A-Za-z0-9_./-]+$ ]]; then
    printf 'Supply a complete branch ref, for example refs/heads/feat/complete-state-coverage.\n' >&2
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

PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=8 "$release/.venv/bin/python" -m pip install -e "${release}[postgres,deployment]"

# Run the Linux-only controls tests with no production mounts, credentials or network.
docker run --rm --network none --cpus 0.5 --memory 768m --memory-swap 768m --cap-drop ALL --security-opt no-new-privileges "election-pipeline:$revision" python -m unittest discover -s tests -v

configuration='/etc/election-pipeline/deployment.env'
if [[ ! -e "${configuration}.before-coverage.${revision}" ]]; then
    cp -p "$configuration" "${configuration}.before-coverage.${revision}"
fi
"$release/.venv/bin/python" - "$release" <<'PY'
import re
import sys
from pathlib import Path

from election_pipeline.deployment.control import private_text

path = Path("/etc/election-pipeline/deployment.env")
text = path.read_text(encoding="utf-8")
if len(re.findall(r"(?m)^CONTROL_ROOT=.*$", text)) != 1:
    raise SystemExit("Expected exactly one CONTROL_ROOT assignment")
private_text(path, re.sub(r"(?m)^CONTROL_ROOT=.*$", "CONTROL_ROOT=" + sys.argv[1], text))
PY
"$release/.venv/bin/python" "$release/scripts/deploy.py" install-controls
printf '\nRelease staged: %s\nExisting collectors and the public dashboard remain running.\n' "$release"
