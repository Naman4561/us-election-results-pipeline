"""Register research datasets or start the local analysis sandbox."""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from election_pipeline.research.catalog import Catalog
from election_pipeline.research.metadata import STATUSES

PARSED_TABLES = {
    "county_results.csv": ("county", "One row per county, contest, and candidate"),
    "contest_results.csv": ("mixed", "One row per contest and candidate across the contest's area"),
    "precinct_results.csv": ("mixed", "One row per reporting unit, contest, and candidate"),
    "reporting_results.csv": ("mixed", "One row per reporting unit, contest, and candidate"),
    "reporting_units.csv": ("mixed", "Reporting units as emitted by the source parser"),
    "results.csv": ("mixed", "One row per source geography, contest, and candidate"),
    "county_feeds.csv": ("county", "One row per county source"),
    "county_demographics.csv": ("county", "County ballot demographic groups by request type"),
    "state_demographics.csv": ("state", "State ballot demographic groups by request type"),
}


def import_parsed(catalog, args):
    report_path = args.directory / "report.json"
    report_bytes = report_path.read_bytes()
    report = json.loads(report_bytes)
    if report["outcome"] != "parsed":
        raise ValueError("Select a successfully parsed snapshot directory")
    dataset = report["dataset"]
    if dataset not in {"election_results", "absentee_early_demographics"}:
        raise ValueError("Use a custom manifest for this parser dataset")
    if not (digest := report["output_sha256"].get(args.table)):
        raise ValueError("The parser report does not list this table")
    geography, grain = PARSED_TABLES[args.table]
    source_id = report["source_id"]
    kind = "demographics" if dataset == "absentee_early_demographics" else "election_results"
    if args.table in {"reporting_units.csv", "county_feeds.csv"}:
        kind = "other"
    manifest = {
        "dataset_id": f"{source_id}.{Path(args.table).stem}",
        "title": f"{source_id} · {Path(args.table).stem.replace('_', ' ').title()}",
        "kind": kind,
        "status": args.status,
        "jurisdiction": args.jurisdiction,
        "geography": geography,
        "grain": grain,
        "period_start": report["election_date"],
        "election_date": report["election_date"],
        "publisher": args.publisher,
        "source_url": report["source_url"],
        "parent_sha256": report["identity"]["raw_sha256"],
        "tags": ["pipeline", dataset],
        "notes": (
            "Parsed pipeline snapshot. Coverage follows the source and selected contests. "
            "Check report.json for reconciliation and scope. Mixed reporting units may overlap."
        ),
    }
    if args.as_of is not None:
        manifest["source_as_of"] = args.as_of
    table_path = args.directory / args.table
    hashes = {table_path: digest, report_path: hashlib.sha256(report_bytes).hexdigest()}
    originals = (report_path,)
    if args.original is not None:
        original = args.original.resolve()
        if original in (table_path.resolve(), report_path.resolve()):
            raise ValueError(
                "The upstream original must be separate from the parsed CSV and report"
            )
        originals += (original,)
        hashes[original] = manifest["parent_sha256"]
    return catalog.import_file(table_path, manifest, originals=originals, expected_hashes=hashes)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("data/research"))
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("import", help="Register a CSV, TSV, or original file")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--manifest", type=Path, required=True)
    ingest.add_argument("--original", type=Path, action="append", default=[])
    ingest.add_argument("--archive-only", action="store_true")

    parsed = commands.add_parser("import-parsed", help="Register a verified pipeline CSV")
    parsed.add_argument("directory", type=Path)
    parsed.add_argument("table", choices=PARSED_TABLES)
    parsed.add_argument("--jurisdiction", required=True)
    parsed.add_argument("--publisher", required=True)
    parsed.add_argument("--status", choices=STATUSES, required=True)
    parsed.add_argument("--as-of", help="Source snapshot timestamp with a UTC offset")
    parsed.add_argument("--original", type=Path, help="Attach a copy of the upstream original")

    commands.add_parser("list", help="List registered versions as JSON")
    serve = commands.add_parser("serve", help="Run the read-only sandbox on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8050)
    args = parser.parse_args(argv)

    try:
        if args.command == "serve":
            import uvicorn

            from election_pipeline.research.server import create_app

            uvicorn.run(create_app(args.store), host="127.0.0.1", port=args.port)
            return 0

        catalog = Catalog(args.store)
        if args.command == "list":
            result = catalog.versions()
        elif args.command == "import-parsed":
            result = import_parsed(catalog, args)
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
            result = catalog.import_file(
                args.file, manifest, originals=tuple(args.original), archive_only=args.archive_only
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, csv.Error, sqlite3.Error) as error:
        print(f"Research catalog: {error}", file=sys.stderr)
        return 1
    except ModuleNotFoundError as error:
        print(f"Install the dashboard extra to serve the sandbox. {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
