"""Re-create one source's live publication from its raw snapshot, inside the release image.

Prints the report as one JSON line (see ``election_pipeline.reproduction``), whose outcome
says whether it reproduced; it exits 0 whenever it printed one.
"""

import argparse
import json
from pathlib import Path

from election_pipeline.database import connection_settings
from election_pipeline.reproduction import reproduce
from election_pipeline.settings import load_config
from election_pipeline.storage import utc_now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--publication-id", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings, sources = load_config(root / "config")
    try:
        report = reproduce(
            root,
            sources[args.source],
            settings,
            root / "data",
            connection_settings(),
            args.publication_id,
        )
    except Exception as error:
        report = {
            "schema_version": 1,
            "source_id": args.source,
            "publication_id": args.publication_id,
            "outcome": "failed",
            "error": str(error),
            "finished_at": utc_now(),
        }
    print(json.dumps(report, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
