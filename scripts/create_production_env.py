"""Generate separate production credentials without printing them."""

import argparse
import os
import re
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"[0-9a-f]{7,40}", args.revision):
        parser.error("Use a Git commit SHA for --revision")

    values = {
        "IMAGE_TAG": args.revision,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "election_pipeline",
        "POSTGRES_USER": "election_admin",
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "PIPELINE_DB_USER": "election_pipeline_writer",
        "PIPELINE_DB_PASSWORD": secrets.token_hex(32),
        "DASHBOARD_DB_USER": "election_dashboard_app",
        "DASHBOARD_DB_PASSWORD": secrets.token_hex(32),
        "DATA_DIR": "/srv/election-pipeline/data",
        "SITE_DOMAIN": "",
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        args.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in values.items()))

    print(f"Created {args.output}; credentials were not printed.")


if __name__ == "__main__":
    main()
