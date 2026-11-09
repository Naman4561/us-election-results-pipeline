"""Operate manual official snapshots through the production deployment catalog."""

import argparse
import json
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.deployment.manual import configure, operate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/election-pipeline/deployment.env")
    )
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("configure")
    setup.add_argument("--source", required=True)
    setup.add_argument("--max-age-hours", type=int, default=6)
    setup.add_argument("--final", action="store_true", help="The source no longer updates")
    local = sub.add_parser("import")
    local.add_argument("--source", required=True)
    local.add_argument("--file", type=Path, required=True)
    local.add_argument("--sha256", required=True)
    local.add_argument("--downloaded-at", required=True)
    local.add_argument("--publish", action="store_true")
    promote = sub.add_parser("promote")
    promote.add_argument("--source", required=True)
    args = parser.parse_args()
    config = dict(dotenv_values(args.config, interpolate=False))
    prod = dict(dotenv_values(config["PRODUCTION_ENV"], interpolate=False))
    try:
        path = Path(config["CATALOG_FILE"])
        if args.action == "configure":
            result = configure(
                path, args.source, max_age_seconds=args.max_age_hours * 3600, final=args.final
            )
        else:
            kwargs = (
                {"promote": True}
                if args.action == "promote"
                else {
                    "file": args.file,
                    "sha256": args.sha256,
                    "downloaded_at": args.downloaded_at,
                    "publish": args.publish,
                }
            )
            result = operate(path, args.source, prod, config, **kwargs)
    except Exception as error:
        result = {"outcome": "failed", "error": str(error)}
    text = json.dumps(result, indent=2)
    for key, value in prod.items():
        if "PASSWORD" in key and value:
            text = text.replace(value, "[redacted]")
    print(text, flush=True)
    return 1 if result["outcome"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
