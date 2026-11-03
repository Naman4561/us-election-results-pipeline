"""Back up the Docker pipeline or rehearse an isolated restore."""

import argparse
import json
import sys
from pathlib import Path

from election_pipeline.database import connection_settings
from election_pipeline.recovery import backup, check_restore


def main():
    root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument(
        "--service",
        required=True,
        help="PostgreSQL service from docker compose config --services",
    )

    commands = parser.add_subparsers(dest="action", required=True)

    create = commands.add_parser("backup")
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--data-dir", type=Path, default=root / "data")
    create.add_argument(
        "--config",
        type=Path,
        default=root / "config/sources.toml",
    )

    restore = commands.add_parser("check-restore")
    restore.add_argument("--backup-dir", type=Path, required=True)

    args = parser.parse_args()
    db = connection_settings(args.env_file)

    try:
        if args.action == "backup":
            result = backup(
                root,
                args.data_dir,
                args.config,
                db,
                args.destination,
                args.service,
            )
        else:
            result = check_restore(
                root,
                db,
                args.backup_dir,
                args.service,
            )

    except Exception as error:
        print(
            str(error).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2))
    return int(result["outcome"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
