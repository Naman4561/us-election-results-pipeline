"""Run exactly one state in its own versioned container."""

import argparse
import json
from pathlib import Path

from election_pipeline.settings import load_config
from election_pipeline.workflow import run_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--status", required=True, choices=("test", "unofficial"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings, sources = load_config(root / "config/sources.toml")
    source = sources[args.source]
    if source.dataset != "election_results":
        raise ValueError("The expansion worker runs results only")
    result = run_source(
        source,
        settings,
        root / "data",
        root=root,
        status=args.status,
        prepare_only=args.prepare_only,
    )
    print(json.dumps(result), flush=True)
    return (
        0
        if result["outcome"]
        in {"succeeded", "already_current", "not_due", "awaiting_publication", "validated"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
