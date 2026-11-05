#!/usr/bin/env bash
# Admit an isolated Mississippi replacement using the existing recovery/handoff gates.
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    printf 'Run with sudo bash.\n' >&2
    exit 1
fi
controlRoot="$(sed -n 's/^CONTROL_ROOT=//p' /etc/election-pipeline/deployment.env)"
if [[ ! "$controlRoot" =~ ^/opt/election-releases/[0-9a-f]{40}$ ]]; then
    printf 'Stage an immutable release before running this script.\n' >&2
    exit 1
fi
revision="${controlRoot##*/}"
python="$controlRoot/.venv/bin/python"
deploy() { "$python" "$controlRoot/scripts/deploy.py" "$@"; }
statusFile="$(mktemp)"
trap 'rm -f "$statusFile"' EXIT
deploy status > "$statusFile"
read -r registered enabled oldOwner oldRevision < <("$python" - "$statusFile" <<'PY'
import json, sys
sources=json.load(open(sys.argv[1], encoding="utf-8"))["sources"]
entry=sources.get("ms_2026_general_all", {})
print(int(bool(entry)), int(entry.get("enabled", False)),
      int("ms_2026_general" in sources), entry.get("revision", "none"))
PY
)
if [[ "$registered" == 1 && "$oldRevision" != "$revision" ]]; then
    printf 'Mississippi replacement is already pinned to another release. Stop here.\n' >&2
    exit 1
fi
if [[ "$enabled" == 1 ]]; then
    printf 'Mississippi replacement is already enabled.\n'
    deploy status
    exit 0
fi
if [[ "$registered" == 0 ]]; then
    arguments=(register --release "$controlRoot" --image "election-pipeline:$revision" --source ms_2026_general_all --status unofficial)
    if [[ "$oldOwner" == 1 ]]; then
        arguments+=(--replaces ms_2026_general)
    fi
    deploy "${arguments[@]}"
fi
deploy migrate --source ms_2026_general_all --file "$controlRoot/migrations/010_ms.sql"
deploy prepare --source ms_2026_general_all
deploy promote --source ms_2026_general_all
# This includes backup, isolated restore, candidate API checks and atomic activation.
deploy activate-sources --source ms_2026_general_all
deploy status
