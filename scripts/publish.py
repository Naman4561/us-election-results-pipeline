"""Publish one completed release, or restore an existing publication."""

import argparse
import json
import sys
import uuid
from pathlib import Path

from election_pipeline.database import connection_settings
from election_pipeline.publication import prepare, publish, publish_prepared, restore


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    commands = parser.add_subparsers(dest="action", required=True)

    for action in ("publish", "transform"):
        candidate = commands.add_parser(action)
        candidate.add_argument("--release-id", type=int, required=True)
        candidate.add_argument("--status", choices=("test", "unofficial"), required=True)
    promotion = commands.add_parser("promote")
    promotion.add_argument("--candidate-id", type=uuid.UUID, required=True)

    previous = commands.add_parser("restore")
    previous.add_argument("--publication-id", type=uuid.UUID, required=True)

    args = parser.parse_args()
    db = connection_settings(args.env_file)

    try:
        if args.action == "promote":
            result = publish_prepared(root, db, args.candidate_id)
        elif args.action == "restore":
            result = restore(db, args.publication_id)
        else:
            function = prepare if args.action == "transform" else publish
            result = function(root, db, args.release_id, args.status)

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
