"""Plan or apply local retention for successfully uploaded backup bundles."""

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

ID_PATTERN = r"backup_[0-9a-f]{32}"


def prune_backups(destination, keep=3, apply=False):
    if keep < 1:
        raise ValueError("Keep at least one uploaded backup")

    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("Backup root must not be a symlink")
    destination = destination.resolve(strict=True)

    candidates = []

    for bundle in destination.iterdir():
        if not re.fullmatch(ID_PATTERN, bundle.name):
            continue

        marker = bundle / "uploaded.json"
        if bundle.is_symlink() or marker.is_symlink():
            raise ValueError(
                "Backup bundles and markers must not be symlinks"
            )
        if not bundle.is_dir() or not marker.is_file():
            continue

        receipt = json.loads(marker.read_text(encoding="utf-8"))
        timestamp = datetime.fromisoformat(receipt["created_at"])

        if (
            receipt.get("schema_version") != 1
            or receipt.get("backup_id") != bundle.name
            or not re.fullmatch(
                r"[0-9a-f]{64}", receipt.get("sha256", "")
            )
            or timestamp.tzinfo is None
        ):
            raise ValueError(
                "Invalid upload marker; cleanup stopped"
            )

        candidates.append((timestamp, bundle.name, bundle))

    candidates.sort(reverse=True)
    removed = [
        bundle for _, _, bundle in candidates[keep:]
    ]

    if apply:
        for bundle in removed:
            if (
                bundle.is_symlink()
                or bundle.resolve().parent != destination
            ):
                raise ValueError(
                    "Backup path changed; cleanup stopped"
                )
            shutil.rmtree(bundle)

    return {
        "outcome": "cleaned" if apply else "planned",
        "kept": [
            name for _, name, _ in candidates[:keep]
        ],
        "removed" if apply else "would_remove": [
            bundle.name for bundle in removed
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("/srv/election-pipeline/backups"),
    )
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        result = prune_backups(
            args.destination, args.keep, args.apply
        )
    except Exception as error:
        print(json.dumps({
            "outcome": "failed",
            "error": str(error),
        }))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())