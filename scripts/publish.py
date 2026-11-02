"""Publish one completed release, or restore an existing publication."""

import argparse
import json
import sys
import uuid
from pathlib import Path

from election_pipeline.database import connection_settings
from election_pipeline.publication import publish, restore


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    commands = parser.add_subparsers(dest="action", required=True)

    candidate = commands.add_parser("publish")
    candidate.add_argument("--release-id", type=int, required=True)
    candidate.add_argument(
        "--status", choices=("test", "unofficial"), required=True
    )

    previous = commands.add_parser("restore")
    previous.add_argument("--publication-id", type=uuid.UUID, required=True)

    args = parser.parse_args()
    db = connection_settings(args.env_file)

    try:
        result = (
            publish(root, db, args.release_id, args.status)
            if args.action == "publish"
            else restore(db, args.publication_id)
        )
    except Exception as exc:
        print(
            str(exc).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())