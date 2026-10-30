"""Collect once; use an external scheduler for repeated five-minute runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from election_pipeline import __version__
from election_pipeline.pipeline import collect_source
from election_pipeline.settings import load_config
from election_pipeline.parsing import parse_source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "list-sources", help="Display configured sources without fetching"
    )
    listing.add_argument("--config", type=Path, default=Path("config/sources.toml"))
    collecting = commands.add_parser("collect", help="Collect one source or all configured sources")
    collecting.add_argument("--config", type=Path, default=Path("config/sources.toml"))
    collecting.add_argument("--data-dir", type=Path, default=Path("data"))
    selection = collecting.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source", help="Source identifier in the TOML configuration")
    selection.add_argument(
        "--all", action="store_true", help="Collect configured sources sequentially"
    )
    collecting.add_argument(
        "--force-download",
        action="store_true",
        help="Skip conditional headers; still honor persisted Retry-After deferrals",
    )
    parsing = commands.add_parser(
        "parse",
        help="Parse and reconcile an archived source; no HTTP",
    )
    parsing.add_argument(
        "--config",
        type=Path,
        default=Path("config/sources.toml"),
    )
    parsing.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
    )
    parsing.add_argument("--source", required=True)
    parsing.add_argument(
        "--sha256",
        help="Replay this archived SHA-256 instead of the latest",
    )
    parsing.add_argument(
        "--reference",
        type=Path,
        help="Independent contest-total CSV to compare",
    )
    args = parser.parse_args(argv)
    try:
        settings, sources = load_config(args.config)
        if args.command == "list-sources":
            for source in sources.values():
                print(
                    json.dumps(
                        {
                            "source_id": source.source_id,
                            "state": source.state,
                            "election_date": source.election_date,
                            "url": source.url,
                        }
                    )
                )
            return 0
        if args.source and args.source not in sources:
            raise ValueError(f"Unknown source: {args.source}; use list-sources")
        if args.command == "parse":
            result = parse_source(
                sources[args.source],
                settings,
                args.data_dir,
                sha256=args.sha256,
                reference=args.reference,
            )

            print(json.dumps(result, sort_keys=True))

            return int(
                result["outcome"] == "validation_failed"
                or result.get("reference_comparison", {}).get("status") == "mismatch"
            )
        selected = [sources[args.source]] if args.source else list(sources.values())
        exit_code = 0
        for source in selected:
            try:
                result = collect_source(
                    source,
                    settings,
                    args.data_dir,
                    force_download=args.force_download,
                )
            except (OSError, ValueError) as error:
                result = {
                    "source_id": source.source_id,
                    "outcome": "local_error",
                    "error": str(error),
                }
            print(json.dumps(result, sort_keys=True))
            if result["outcome"] not in {
                "downloaded",
                "unchanged",
                "not_due",
                "awaiting_publication",
            }:
                exit_code = 1
        return exit_code
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"outcome": "configuration_error", "error": str(error)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"outcome": "interrupted"}), file=sys.stderr)
        return 130
