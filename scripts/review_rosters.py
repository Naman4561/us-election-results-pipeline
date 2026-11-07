"""Show the candidate and county roster a captured official snapshot implies.

Parses one archived bundle with roster checks switched to recording, then prints, per
race, what differs from the reviewed roster in ``resources/rosters_2026/<STATE>.json``.
With ``--write`` it replaces that state's races with the observed ones (keeping the
provenance note, which should then be updated by hand to name the capture). Nothing is
fetched; review every change against the state's own results pages before committing.

    python scripts/review_rosters.py --source va_2026_general --archive path/to/bundle.zip
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from election_pipeline.parsers.scope import recording
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.settings import load_config
from election_pipeline.states import parser_for

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "src/election_pipeline/resources/rosters_2026"


def observe(source, settings, archive):
    with tempfile.TemporaryDirectory() as directory, recording() as observed:
        parser_for(source)(Path(archive), source, settings, Path(directory), {})
    return observed.get(source.state, {})


def differences(state, observed):
    reviewed = ROSTERS.get(state, {})
    changes = {}
    for race in sorted(set(reviewed) | set(observed)):
        old, new = reviewed.get(race), observed.get(race)
        if old is None or new is None:
            changes[race] = "added" if old is None else "missing from snapshot"
            continue
        entry = {}
        for key in ("candidates", "counties"):
            added = sorted(set(new[key]) - set(old[key]))
            removed = sorted(set(old[key]) - set(new[key]))
            if added or removed:
                entry[key] = {"added": added, "removed": removed}
        if entry:
            changes[race] = entry
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "config/sources.toml")
    parser.add_argument("--write", action="store_true", help="Replace the state's roster")
    args = parser.parse_args()
    settings, sources = load_config(args.config)
    source = sources[args.source]
    observed = observe(source, settings, args.archive)
    print(
        json.dumps(
            {"state": source.state, "differences": differences(source.state, observed)}, indent=2
        )
    )
    if args.write:
        path = FOLDER / f"{source.state}.json"
        document = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"state": source.state, "provenance": "Describe the reviewed capture here."}
        )
        document["races"] = observed
        path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}; update its provenance note.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
