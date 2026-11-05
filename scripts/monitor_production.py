"""Check production reports, publish SNS alerts and a CloudWatch heartbeat."""

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.settings import load_config
from election_pipeline.storage import atomic_json

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path("/var/lib/election-pipeline-monitor")
GOOD_RUNS = {"succeeded", "already_current", "not_due", "awaiting_publication"}


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def age_seconds(value, now):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or (timestamp - now).total_seconds() > 60:
        raise ValueError("Invalid timestamp")
    return (now - timestamp).total_seconds()


def report_problem(path, allowed, max_age, now, allow_running=False):
    try:
        report = read_json(path)
        running = allow_running and report.get("outcome") == "running"
        stamp = report["started_at" if running else "finished_at"]
        if age_seconds(stamp, now) > max_age:
            return "Run is overdue or has been running too long"
        if not running and report.get("outcome") not in allowed:
            return "Latest run did not complete successfully"
    except (OSError, ValueError, TypeError, KeyError):
        return "Run report is missing or invalid"
    return None


def systemd(action, unit):
    result = subprocess.run(
        ["systemctl", action, "--quiet", unit],
        capture_output=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def dashboard_payload(url):
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


def assess(prod, config, now):
    data = Path(prod["DATA_DIR"])
    _, configured = load_config(ROOT / "config/sources.toml")
    names = [name.strip() for name in prod["PIPELINE_SOURCES"].split(",") if name.strip()]
    catalog = None
    if config.get("DEPLOYMENT_CATALOG"):
        from election_pipeline.deployment import catalog as registry

        catalog = registry.read(config["DEPLOYMENT_CATALOG"])
        registry.verify_baseline(catalog, prod)
        configured = registry.sources(catalog)
        names = [name for name, entry in catalog["sources"].items() if entry["enabled"]]
    if not names or len(names) != len(set(names)) or any(name not in configured for name in names):
        raise ValueError("Invalid PIPELINE_SOURCES configuration")

    issues = {}
    if catalog:
        from election_pipeline.deployment.containers import inspect_image

        try:
            identity, _ = inspect_image("election-pipeline:" + prod["IMAGE_TAG"])
            if identity != catalog["baseline"]["image_id"]:
                issues["deployment/baseline"] = "The existing collector image tag was replaced"
        except Exception:
            issues["deployment/baseline"] = "Cannot verify the existing collector image"
        journal = Path(config["DEPLOYMENT_CATALOG"]).parent / "handoff.json"
        if journal.exists():
            try:
                handoff = read_json(journal)
                if (
                    handoff.get("outcome") == "pending"
                    and age_seconds(handoff["started_at"], now) > 120
                ):
                    issues["deployment/handoff"] = "Dashboard handoff requires recovery"
            except (ValueError, OSError, TypeError, KeyError):
                issues["deployment/handoff"] = "Dashboard handoff journal is invalid"
    for name in names:
        source = configured[name]
        if catalog and catalog["sources"][name]["worker"] == "manual":
            entry = catalog["sources"][name]
            try:
                receipt = entry["manual_snapshot"]
                if age_seconds(receipt["downloaded_at"], now) > entry.get(
                    "manual_max_age_seconds", 21600
                ):
                    issues[f"manual/{name}"] = "Manual snapshot is overdue for an operator update"
                if receipt["publication_id"] != entry["publication_id"]:
                    issues[f"manual/{name}"] = (
                        "Manual receipt does not match the approved publication"
                    )
            except (KeyError, ValueError, TypeError):
                issues[f"manual/{name}"] = "Manual publication receipt is missing or invalid"
            continue
        run_limit = int(config.get("MAX_RUN_AGE_SECONDS", "900"))
        poll_limit = max(run_limit, source.min_interval_seconds * 3)

        problem = report_problem(
            data / "runs" / name / "latest.json",
            GOOD_RUNS,
            run_limit,
            now,
            allow_running=True,
        )
        if problem:
            issues[f"pipeline/{name}"] = problem

        try:
            state = read_json(data / "state" / f"{name}.json")
            if age_seconds(state["last_checked_at"], now) > poll_limit:
                issues[f"poll/{name}"] = "No recent successful official-source check"
        except (OSError, ValueError, TypeError, KeyError):
            issues[f"poll/{name}"] = "Collector state is missing or invalid"

    problem = report_problem(
        Path(config.get("BACKUP_DIR", "/srv/election-pipeline/backups")) / "latest-backup.json",
        {"uploaded"},
        int(config.get("MAX_BACKUP_AGE_SECONDS", "7200")),
        now,
    )
    if problem:
        issues["backup"] = problem

    for timer in ("election-pipeline.timer", "election-backup.timer"):
        if not systemd("is-active", timer):
            issues[f"timer/{timer}"] = "Timer is not active"

    for service in ("election-pipeline.service", "election-backup.service"):
        if systemd("is-failed", service):
            issues[f"service/{service}"] = "Service is in a failed state"

    if catalog and any(
        e["enabled"] and e["worker"] == "expansion" for e in catalog["sources"].values()
    ):
        if not systemd("is-active", "election-expansion.timer"):
            issues["timer/election-expansion.timer"] = "Expansion timer is not active"
        if systemd("is-failed", "election-expansion.service"):
            issues["service/election-expansion.service"] = "Expansion worker failed"

    try:
        url = config.get("DASHBOARD_URL", "http://127.0.0.1:8000/api/snapshot")
        if catalog:
            url = config.get("DASHBOARD_URL", "http://127.0.0.1/api/national").replace(
                "/api/snapshot", "/api/national"
            )
        payload = dashboard_payload(url)
        if "states" in payload:
            if catalog and set(payload["states"]) != registry.public_states(catalog):
                issues["dashboard/coverage"] = "Public dashboard coverage differs from approval"
            if payload.get("mode") != "published":
                issues["dashboard"] = "Dashboard is not serving published returns"
            for name in names:
                if catalog and not catalog["sources"][name]["public"]:
                    continue
                code = configured[name].state
                state = payload["states"].get(code, {})
                if state.get("availability") != "available" or not state.get("races"):
                    issues[f"dashboard/{code}"] = "State dashboard results are unavailable"
                elif config.get("REQUIRE_PUBLICATION", "true").lower() == "true" and not state.get(
                    "publication"
                ):
                    issues[f"dashboard/{code}"] = "State dashboard has no approved publication"
                for county, record in state.get("county_feeds", {}).items():
                    if record.get("issue") == "manual_snapshot":
                        if age_seconds(record.get("last_success_at"), now) > 21600:
                            issues[f"county/{code}/{county}"] = (
                                "Manual county snapshot is over six hours old"
                            )
                        continue
                    if record.get("collection_status") in {"stale", "unavailable"}:
                        issues[f"county/{code}/{county}"] = (
                            "County source requires scan review"
                            if record.get("issue") == "review_required"
                            else "County source check is overdue or failed; "
                            "last accepted votes retained"
                        )
        elif catalog:
            issues["dashboard"] = "Deployment monitoring requires the national dashboard API"
        elif (
            payload.get("mode") != "published"
            or not isinstance(payload.get("races"), dict)
            or not payload["races"]
        ):
            issues["dashboard"] = "Dashboard returned an unexpected payload"
        elif config.get("REQUIRE_PUBLICATION", "true").lower() == "true" and not payload.get(
            "publication"
        ):
            issues["dashboard"] = "Dashboard has no approved publication"
    except Exception:
        issues["dashboard"] = "Dashboard API is unavailable or invalid"

    minimum = float(config.get("MIN_FREE_DISK_PERCENT", "15"))
    if not 0 < minimum < 100:
        raise ValueError("MIN_FREE_DISK_PERCENT must be between 0 and 100")

    for path in config.get("DISK_PATHS", "/").split(","):
        path = path.strip()
        try:
            usage = shutil.disk_usage(path)
            if usage.free / usage.total * 100 < minimum:
                issues[f"disk/{path}"] = "Free disk space is below the configured threshold"
        except OSError:
            issues[f"disk/{path}"] = "Cannot inspect disk space"

    return issues


def aws(config, *arguments):
    result = subprocess.run(
        [
            "/usr/local/bin/aws",
            "--profile",
            config.get("AWS_PROFILE", "election-backup"),
            "--region",
            config.get("AWS_DEFAULT_REGION", "us-east-2"),
            "--cli-connect-timeout",
            "5",
            "--cli-read-timeout",
            "10",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "AWS_MAX_ATTEMPTS": "2", "AWS_PAGER": ""},
    )
    if result.returncode:
        raise RuntimeError("AWS notification/heartbeat request failed: " + result.stderr[-1500:])


def publish(config, subject, body):
    aws(
        config,
        "sns",
        "publish",
        "--topic-arn",
        config["SNS_TOPIC_ARN"],
        "--subject",
        subject[:100],
        "--message",
        body,
    )


def notify_changes(config, issues, previous, now):
    old_issues = previous.get("issues", {})
    last_sent = previous.get("last_sent")
    reminder_due = issues and (
        not last_sent or age_seconds(last_sent, now) >= int(config.get("REMINDER_SECONDS", "1800"))
    )

    if issues == old_issues and not reminder_due:
        return previous, False

    status = "ALERT" if issues else "RECOVERED"
    body = json.dumps(
        {
            "deployment": config["DEPLOYMENT"],
            "checked_at": now.isoformat(),
            "issues": issues,
            "cleared": sorted(set(old_issues) - set(issues)),
        },
        indent=2,
    )
    publish(config, f"{status}: {config['DEPLOYMENT']}", body)
    return {"issues": issues, "last_sent": now.isoformat()}, True


def heartbeat(config):
    metric = [
        {
            "MetricName": "MonitorHeartbeat",
            "Value": 1,
            "Unit": "Count",
            "Dimensions": [{"Name": "Deployment", "Value": config["DEPLOYMENT"]}],
        }
    ]
    aws(
        config,
        "cloudwatch",
        "put-metric-data",
        "--namespace",
        "ElectionPipeline",
        "--metric-data",
        json.dumps(metric),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/etc/election-pipeline/production.env"),
    )
    parser.add_argument(
        "--monitor-env-file",
        type=Path,
        default=Path("/etc/election-pipeline/monitor.env"),
    )
    parser.add_argument("--catalog-file", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()

    os.umask(0o077)
    prod = dict(dotenv_values(args.env_file, interpolate=False))
    config = dict(dotenv_values(args.monitor_env_file, interpolate=False))
    if args.catalog_file:
        config["DEPLOYMENT_CATALOG"] = str(args.catalog_file)
    config.setdefault("DEPLOYMENT", "election-production")
    secrets = [value for key, value in prod.items() if "PASSWORD" in key and value]
    if config.get("SNS_TOPIC_ARN"):
        secrets.append(config["SNS_TOPIC_ARN"])

    try:
        if not config.get("SNS_TOPIC_ARN", "").startswith("arn:"):
            raise ValueError("Set SNS_TOPIC_ARN in monitor.env")

        if args.test_alert:
            publish(
                config,
                f"TEST: {config['DEPLOYMENT']}",
                "Election pipeline notification test.",
            )
            result = {"outcome": "test_sent"}
        else:
            now = datetime.now(timezone.utc)
            issues = assess(prod, config, now)
            result = {
                "outcome": "unhealthy" if issues else "healthy",
                "issues": issues,
                "checked_at": now.isoformat(),
            }

            if not args.dry_run:
                STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = STATE_DIR / "state.json"
                previous = read_json(path) if path.exists() else {}
                updated, notified = notify_changes(config, issues, previous, now)
                atomic_json(path, updated)
                heartbeat(config)
                result["notified"] = notified
                atomic_json(STATE_DIR / "latest-check.json", result)

    except Exception as error:
        text = str(error)
        for secret in secrets:
            text = text.replace(secret, "[redacted]")
        print(json.dumps({"outcome": "monitor_failed", "error": text}), flush=True)
        return 1

    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
