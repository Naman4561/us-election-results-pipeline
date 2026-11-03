"""Record official-reference agreement for one immutable publication."""

import argparse
import json
import sys
import uuid
from pathlib import Path

from election_pipeline.accuracy import compare_publication
from election_pipeline.database import connection_settings


def main():
    root = Path(__file__).resolve().parents[1]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--publication-id", type=uuid.UUID, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-url", required=True)
    parser.add_argument(
        "--observed-at",
        required=True,
        help="ISO timestamp with time-zone offset",
    )
    parser.add_argument("--same-reporting-cutoff", action="store_true")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=root / "data/accuracy",
    )

    args = parser.parse_args()
    db = connection_settings(args.env_file)

    try:
        result = compare_publication(
            db,
            args.publication_id,
            args.reference,
            evidence_dir=args.evidence_dir,
            reference_url=args.reference_url,
            observed_at=args.observed_at,
            same_reporting_cutoff=args.same_reporting_cutoff,
        )
    except Exception as error:
        print(
            str(error).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, indent=2))
    return int(result["outcome"] != "matched")


if __name__ == "__main__":
    raise SystemExit(main())
