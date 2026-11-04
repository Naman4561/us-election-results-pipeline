"""Use the pinned image's offline import, parser, database and publication workflow."""

import argparse
import json
from pathlib import Path

from election_pipeline.manual import import_file
from election_pipeline.settings import load_config
from election_pipeline.storage import atomic_json
from election_pipeline.workflow import run_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--downloaded-at", required=True)
    parser.add_argument("--minimum-generated-at")
    parser.add_argument("--status", choices=("test", "unofficial"), required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings, sources = load_config(root / "config/sources.toml")
    source = sources[args.source]
    try:
        args.file.resolve(strict=True).relative_to((root / "data/inbox").resolve())
        receipt = import_file(
            source,
            settings,
            root / "data",
            args.file,
            args.downloaded_at,
            args.sha256,
            minimum=args.minimum_generated_at,
        )
        result = run_source(
            source,
            settings,
            root / "data",
            root=root,
            status=args.status,
            replay_sha256=receipt["sha256"],
            prepare_only=not args.publish,
            publish_replay=args.publish,
        )
        result.update(mode="manual", manual_acquisition=receipt)
        if result["outcome"] != "locked":
            atomic_json(Path(result["report_path"]), result)
            atomic_json(root / "data/runs" / source.source_id / "latest.json", result)
    except Exception as error:
        result = {"outcome": "failed", "source_id": args.source, "error": str(error)}
    print(json.dumps(result), flush=True)
    return 0 if result["outcome"] in ("validated", "succeeded", "already_current") else 1


if __name__ == "__main__":
    raise SystemExit(main())
