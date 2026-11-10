"""Operate the production deployment: the worker cycle, releases, sources and dashboard."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.deployment import catalog, control, dashboard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/election-pipeline/deployment.env")
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run", help="One worker cycle over every enabled HTTP source")
    sub.add_parser("status")
    staged = sub.add_parser("release", help="Make a staged release current")
    staged.add_argument("--release", type=Path, required=True)
    staged.add_argument("--image", required=True)
    staged.add_argument(
        "--moved-source",
        action="append",
        default=[],
        help="A source whose address this release changes; it has no snapshot to check yet",
    )
    sub.add_parser("rollback-release")
    for name in ("register", "prepare", "promote", "disable", "pause", "resume"):
        sub.add_parser(name).add_argument("--source", required=True)
    certify = sub.add_parser("certify", help="Publish a source's certified results")
    certify.add_argument("--source", required=True)
    certify.add_argument(
        "--approve-snapshot",
        metavar="RAW_SHA256",
        help="Attest that this published snapshot is the state's certified result",
    )
    certify.add_argument(
        "--certified-file",
        type=Path,
        help="Import the state's certified file, placed under the data inbox",
    )
    certify.add_argument("--certified-name", default="certified-results.csv")
    certify.add_argument("--sha256", help="The certified file's SHA-256 where downloaded")
    certify.add_argument("--downloaded-at", help="When it was downloaded (UTC, ISO 8601)")
    certify.add_argument("--evidence-url", help="The state's certification notice or file")
    certify.add_argument("--certified-on", help="The state's certification date (YYYY-MM-DD)")
    certify.add_argument("--attested-by", help="Who checked and attests, as they sign")
    county = sub.add_parser("import-county", help="Import a reviewed Mississippi county report")
    county.add_argument("--county", required=True)
    county.add_argument("--file", type=Path, required=True)
    county.add_argument("--review-json", type=Path, required=True)
    start = sub.add_parser("start-dashboard", help="A private candidate from the release")
    start.add_argument("--source", action="append", default=[], help="Also make public")
    activate = sub.add_parser("activate")
    activate.add_argument("--candidate", required=True)
    activate.add_argument("--restore-report", type=Path)
    sub.add_parser("rollback-dashboard")
    sub.add_parser("recover-handoff")
    sub.add_parser("discard-dashboard").add_argument("--name", required=True)
    args = parser.parse_args()
    config = dict(dotenv_values(args.config, interpolate=False))
    prod = dict(dotenv_values(config["PRODUCTION_ENV"], interpolate=False))
    path = Path(config["CATALOG_FILE"])
    state = Path(config["CONTROL_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.umask(0o077)
    try:
        action = args.action
        if action == "run":
            # systemd prevents overlapping cycles; this lock also covers manual invocations.
            with catalog.locked(state / "worker.json", exclusive=True, nonblocking=True):
                return control.run_enabled(path, prod, config)
        if action == "status":
            value = catalog.read(path)
            result = {
                "generation": value["generation"],
                "release": value["release"]["revision"],
                "dashboard": value["dashboard"]["name"],
                "sources": {
                    n: {k: e[k] for k in ("acquisition", "enabled", "public", "status")}
                    for n, e in value["sources"].items()
                },
            }
        elif action == "release":
            result = control.release(
                path, args.release, args.image, prod, config, args.moved_source
            )
        elif action == "rollback-release":
            result = control.rollback_release(path, config)
        elif action in ("prepare", "promote"):
            result = getattr(control, action)(path, args.source, prod, config)
        elif action == "register":
            result = control.register(path, args.source)
        elif action == "disable":
            result = control.disable(path, args.source)
        elif action in ("pause", "resume"):
            result = control.pause(path, args.source, action == "pause")
        elif action == "certify":
            attested = certified = None
            if args.certified_file:
                certified = {
                    "file": args.certified_file,
                    "sha256": args.sha256,
                    "downloaded_at": args.downloaded_at,
                    "name": args.certified_name,
                }
            if args.approve_snapshot or args.certified_file:
                attested = {
                    "evidence_url": args.evidence_url,
                    "certified_on": args.certified_on,
                    "attested_by": args.attested_by,
                    "attested_at": datetime.now(timezone.utc).isoformat(),
                }
            result = control.certify(
                path,
                args.source,
                prod,
                config,
                approve=args.approve_snapshot,
                certified=certified,
                attested=attested,
            )
        elif action == "import-county":
            result = control.import_county(
                path, args.county, args.file, args.review_json, prod, config
            )
        elif action == "start-dashboard":
            result = control.start_dashboard(path, args.source, prod, config)
        elif action == "activate":
            candidate = json.loads(
                (state / f"candidate-{args.candidate}.json").read_text(encoding="utf-8")
            )
            report = (
                json.loads(args.restore_report.read_text(encoding="utf-8"))
                if args.restore_report
                else {}
            )
            result = control.activate(path, candidate, report, config)
        elif action == "rollback-dashboard":
            result = control.rollback_dashboard(path, config)
        elif action == "recover-handoff":
            with catalog.locked(path, exclusive=True):
                dashboard.recover(state / "handoff.json")
            result = {"outcome": "recovered"}
        else:
            dashboard.stop_unused(args.name, catalog.read(path))
            result = {"outcome": "discarded"}
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as error:
        print(
            json.dumps({"outcome": "failed", "error": control.redacted(str(error), prod)}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
