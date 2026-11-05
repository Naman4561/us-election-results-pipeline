"""Manage isolated expansion releases without restarting existing collectors."""

import argparse
import json
import os
import uuid
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.deployment import catalog, control, coverage, dashboard
from election_pipeline.storage import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/election-pipeline/deployment.env")
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init")
    sub.add_parser("install-controls")
    sub.add_parser("status")
    sub.add_parser("run")
    sub.add_parser("rollback-dashboard")
    sub.add_parser("recover-handoff")
    sub.add_parser("recover-coverage-retirement")
    admitted = sub.add_parser("activate-sources")
    admitted.add_argument("--source", action="append", required=True)
    expand = sub.add_parser("coverage")
    expand.add_argument("phase", choices=("prepare", "activate", "retire"))
    expand.add_argument("--states", nargs="+", required=True, choices=sorted(coverage.STATES))
    expand.add_argument("--release", type=Path)
    expand.add_argument("--image")
    register = sub.add_parser("register")
    register.add_argument("--release", type=Path, required=True)
    register.add_argument("--image", required=True)
    register.add_argument("--source", required=True)
    register.add_argument("--status", choices=("test", "unofficial"), required=True)
    register.add_argument(
        "--replaces", help="Existing source whose race coverage is being expanded"
    )
    transfer = sub.add_parser("transfer-source")
    transfer.add_argument("--release", type=Path, required=True)
    transfer.add_argument("--image", required=True)
    transfer.add_argument("--source", required=True)
    transfer.add_argument("--status", choices=("test", "unofficial"), required=True)
    transfer.add_argument("--evidence-file", type=Path, required=True)
    transfer.add_argument("--validation-report", type=Path, required=True)
    for name in ("migrate", "prepare", "promote"):
        command = sub.add_parser(name)
        command.add_argument("--source", required=True)
        if name == "migrate":
            command.add_argument("--file", type=Path, required=True)
    start = sub.add_parser("start-dashboard")
    start.add_argument("--source", action="append", default=[])
    start.add_argument("--image", required=True)
    start.add_argument("--port", type=int, default=8001)
    activate = sub.add_parser("activate")
    activate.add_argument("--candidate", type=uuid.UUID, required=True)
    activate.add_argument("--restore-report", type=Path)
    discard = sub.add_parser("discard-dashboard")
    discard.add_argument("--name", required=True)
    args = parser.parse_args()
    config = dict(dotenv_values(args.config, interpolate=False))
    prod = dict(
        dotenv_values(
            config.get("PRODUCTION_ENV", "/etc/election-pipeline/production.env"), interpolate=False
        )
    )
    path = Path(config["CATALOG_FILE"])
    state = Path(config["CONTROL_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.umask(0o077)
    try:
        action = args.action
        if action == "init":
            result = control.initialize(path, config["BASELINE_ROOT"], prod)
        elif action == "install-controls":
            result = control.install_controls(config)
        elif action == "status":
            value = catalog.read(path)
            result = {
                "generation": value["generation"],
                "sources": {
                    n: {k: e[k] for k in ("worker", "enabled", "public", "revision")}
                    for n, e in value["sources"].items()
                },
            }
        elif action == "register":
            result = control.register(
                path,
                args.release,
                args.image,
                args.source,
                args.status,
                prod,
                config,
                replaces=args.replaces,
            )
        elif action == "activate-sources":
            result = coverage.activate_sources(path, args.source, prod, config)
        elif action == "coverage":
            if args.phase == "prepare":
                if not args.release or not args.image:
                    raise ValueError("Coverage preparation requires --release and --image")
                result = coverage.prepare(path, args.states, args.release, args.image, prod, config)
            else:
                result = getattr(coverage, args.phase)(path, args.states, prod, config)
        elif action == "recover-coverage-retirement":
            result = coverage.recover_retirement(path, config)
        elif action == "transfer-source":
            result = control.transfer_source(
                path,
                args.release,
                args.image,
                args.source,
                args.status,
                args.evidence_file,
                args.validation_report,
                config,
            )
        elif action == "migrate":
            result = control.migrate_source(path, args.source, args.file, prod)
        elif action in ("prepare", "promote"):
            result = getattr(control, action)(path, args.source, prod, config)
        elif action == "start-dashboard":
            identity = uuid.uuid4().hex
            result = dashboard.start(
                catalog.read(path),
                args.source,
                prod,
                config,
                state / ("dashboard-" + identity),
                port=args.port,
                image=args.image,
            )
            result["id"] = identity
            atomic_json(state / ("candidate-" + identity + ".json"), result)
        elif action == "activate":
            candidate = json.loads(
                (state / ("candidate-" + args.candidate.hex + ".json")).read_text(encoding="utf-8")
            )
            report = (
                json.loads(args.restore_report.read_text(encoding="utf-8"))
                if args.restore_report
                else {}
            )
            result = control.activate(path, candidate, report, prod, config)
        elif action == "rollback-dashboard":
            result = control.rollback(path, config)
        elif action == "recover-handoff":
            with catalog.locked(path, exclusive=True):
                dashboard.recover(state / "handoff.json")
            result = {"outcome": "recovered"}
        elif action == "discard-dashboard":
            dashboard.stop_unused(args.name, catalog.read(path))
            result = {"outcome": "discarded"}
        else:
            # systemd already prevents overlapping activations of this unit. Also protect
            # manual invocations with a separate lock so backups can still read the catalog.
            with catalog.locked(state / "worker.json", exclusive=True, nonblocking=True):
                return control.run_enabled(path, prod, config)
        print(json.dumps(result, indent=2), flush=True)
        return 0
    except Exception as error:
        text = str(error)
        for key, secret in prod.items():
            if "PASSWORD" in key and secret:
                text = text.replace(secret, "[redacted]")
        print(json.dumps({"outcome": "failed", "error": text}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
