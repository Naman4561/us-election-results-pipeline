"""Check production reports, publish SNS alerts and a CloudWatch heartbeat.

An overdue manual snapshot is an operator task, not a production or collection fault:
it is reported as a reminder (key prefix ``reminder/``), sent as its own ``REMINDER``
message and left out of the health outcome, alerts and recoveries.
"""

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from election_pipeline.storage import atomic_json

STATE_DIR = Path("/var/lib/election-pipeline-monitor")
GOOD_RUNS = {"succeeded", "already_current", "not_due", "awaiting_publication"}
REMINDER = "reminder/"


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def official_available(data, name):
    """Whether the latest run parsed a snapshot its state marks as official."""
    try:
        run = read_json(data / "runs" / name / "latest.json")
    except (OSError, ValueError):
        return False
    return any(
        s.get("stage") == "parse" and (s.get("result") or {}).get("official_results") is True
        for s in run.get("stages", [])
    )


def age_seconds(value, now):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None or (timestamp - now).total_seconds() > 60:
        raise ValueError("Invalid timestamp")
    return (now - timestamp).total_seconds()


def failure(report):
    """What a run report says went wrong: its outcome, failing stage and error."""
    stages = [s for s in report.get("stages") or [] if isinstance(s, dict)]
    stage = next((s for s in reversed(stages) if s.get("error")), stages[-1] if stages else None)
    error = report.get("error") or (stage or {}).get("error")
    text = f"Latest run {report.get('outcome') or 'ended without an outcome'}"
    if stage:
        text += f" at {stage.get('stage')} ({stage.get('outcome')})"
    if error:
        text += ": " + " ".join(str(error).split())[:300]
    return text


def report_problem(path, allowed, max_age, now, allow_running=False):
    try:
        report = read_json(path)
        running = allow_running and report.get("outcome") == "running"
        stamp = report["started_at" if running else "finished_at"]
        if age_seconds(stamp, now) > max_age:
            return "Run is overdue or has been running too long"
        if not running and report.get("outcome") not in allowed:
            return failure(report)
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
    # The dashboard answers from its last payload while refreshing; a slow answer means
    # the host is overloaded, which the other checks and the next run will show.
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.load(response)


def assess(prod, config, now):
    from election_pipeline.deployment import catalog as registry
    from election_pipeline.deployment.containers import inspect_image

    data = Path(prod["DATA_DIR"])
    catalog = registry.read(config["CATALOG_FILE"])
    configured = registry.configured()
    names = sorted(name for name, entry in catalog["sources"].items() if entry["enabled"])

    issues = {}
    release = catalog["release"]
    try:
        if inspect_image(release["image_id"])[1] != release["revision"]:
            issues["deployment/release"] = "The release image has a different revision"
    except Exception:
        issues["deployment/release"] = "The release image is missing"
    current = config.get("CURRENT_RELEASE")
    if current and Path(current).resolve() != Path(release["release_path"]).resolve():
        issues["deployment/current"] = "The current symlink is not the catalog's release"
    journal = Path(config["CATALOG_FILE"]).parent / "handoff.json"
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
        entry = catalog["sources"][name]
        if entry["acquisition"] == "manual":
            try:
                receipt = entry["manual_snapshot"]
                if entry.get("manual_final") is not True and age_seconds(
                    receipt["downloaded_at"], now
                ) > entry.get("manual_max_age_seconds", 21600):
                    issues[f"{REMINDER}manual/{name}"] = (
                        "Manual snapshot is overdue for an operator update"
                    )
                if receipt["publication_id"] != entry["publication_id"]:
                    issues[f"manual/{name}"] = (
                        "Manual receipt does not match the approved publication"
                    )
            except (KeyError, ValueError, TypeError):
                issues[f"manual/{name}"] = "Manual publication receipt is missing or invalid"
            continue
        if entry["status"] != "official" and official_available(data, name):
            issues[f"{REMINDER}official/{name}"] = (
                "The state's source marks its results official; check them, then certify"
            )
        if entry["public"] and entry["status"] == "test":
            issues[f"status/{name}"] = (
                "Public source is labelled test; deploy mark-unofficial relabels it"
            )
        if entry.get("paused"):
            # An operator stopped its collection; its publication is still checked below.
            # A certified source stays paused for good, which needs no reminder.
            if entry["status"] != "official":
                issues[f"{REMINDER}paused/{name}"] = "Collection is paused by an operator"
            continue
        run_limit = int(config.get("MAX_RUN_AGE_SECONDS", "3600"))
        poll_limit = max(run_limit, source.poll_seconds * 3)

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
            # One transient failure is normal; repeated failures or a refusal are not.
            if (state.get("consecutive_failures") or 0) >= 3 or state.get("blocked_since"):
                issues[f"poll/{name}"] = (
                    f"Collection failing: {state['consecutive_failures']} consecutive runs"
                    + (" refused by the site" if state.get("blocked_since") else "")
                    + f"; last: {str(state.get('last_failure'))[:120]}"
                    + f"; next try after {state.get('failure_backoff_until')}"
                )
        except (OSError, ValueError, TypeError, KeyError):
            issues[f"poll/{name}"] = "Collector state is missing or invalid"

    backups = Path(config.get("BACKUP_DIR", "/srv/election-pipeline/backups"))
    backup_age = int(config.get("MAX_BACKUP_AGE_SECONDS", "25200"))
    problem = report_problem(backups / "latest-backup.json", {"uploaded"}, backup_age, now)
    if problem:
        # A backup skipped because nothing new arrived keeps the last upload current, for
        # up to a day (production_recovery.py --skip-unchanged).
        try:
            latest = read_json(backups / "latest-backup.json")
            skipped = read_json(backups / "latest-backup-unchanged.json")
            if (
                latest["outcome"] == "uploaded"
                and skipped["outcome"] == "unchanged"
                and skipped["backup_id"] == latest["backup_id"]
                and age_seconds(skipped["finished_at"], now) <= backup_age
                and age_seconds(latest["finished_at"], now) <= 24 * 3600 + backup_age
            ):
                problem = None
        except (OSError, ValueError, TypeError, KeyError):
            pass
    if problem:
        issues["backup"] = problem
    else:
        retention = read_json(
            Path(config.get("BACKUP_DIR", "/srv/election-pipeline/backups")) / "latest-backup.json"
        ).get("s3_retention")
        if retention and retention.get("outcome") != "applied":
            issues["backup/s3-retention"] = "Older S3 backups were not marked for expiry"

    # A failed service points at the reports that say why.
    causes = {
        "election-worker": sorted(k for k in issues if k.startswith("pipeline/")),
        "election-backup": [k for k in ("backup",) if k in issues],
    }
    for unit in ("election-worker", "election-backup"):
        if not systemd("is-active", unit + ".timer"):
            issues[f"timer/{unit}.timer"] = "Timer is not active"
        if systemd("is-failed", unit + ".service"):
            issues[f"service/{unit}.service"] = "Service is in a failed state" + (
                "; see " + ", ".join(causes[unit]) if causes[unit] else ""
            )

    try:
        payload = dashboard_payload(config.get("DASHBOARD_URL", "http://127.0.0.1/api/national"))
        if set(payload.get("states", {})) != registry.public_states(catalog):
            issues["dashboard/coverage"] = "Public dashboard coverage differs from approval"
        if payload.get("mode") != "published":
            issues["dashboard"] = "Dashboard is not serving published returns"
        for name in names:
            if not catalog["sources"][name]["public"]:
                continue
            code = configured[name].state
            state = payload["states"].get(code, {})
            if state.get("availability") != "available" or not state.get("races"):
                issues[f"dashboard/{code}"] = "State dashboard results are unavailable"
            elif not state.get("publication"):
                issues[f"dashboard/{code}"] = "State dashboard has no approved publication"
            final = set(catalog["sources"][name].get("manual_final_counties", []))
            for county, record in state.get("county_feeds", {}).items():
                if record.get("issue") == "manual_snapshot":
                    # A county whose last report before certification is in is never due.
                    if (
                        county not in final
                        and age_seconds(record.get("last_success_at"), now) > 21600
                    ):
                        issues[f"{REMINDER}county/{code}/{county}"] = (
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


def split(found):
    """(alerts, reminders): reminders lose their prefix."""
    alerts = {key: value for key, value in found.items() if not key.startswith(REMINDER)}
    reminders = {
        key[len(REMINDER) :]: value for key, value in found.items() if key.startswith(REMINDER)
    }
    return alerts, reminders


def notify_reminders(config, reminders, previous, now):
    """Send operator reminders when one is new, then every MANUAL_REMINDER_SECONDS while
    any stay open. Clearing a reminder sends nothing."""
    last_sent = previous.get("last_sent")
    new = set(reminders) - set(previous.get("reminders", {}))
    due = not last_sent or age_seconds(last_sent, now) >= int(
        config.get("MANUAL_REMINDER_SECONDS", "21600")
    )
    if not reminders:
        return {}, False
    if not new and not due:
        return {**previous, "reminders": reminders}, False
    body = json.dumps(
        {
            "deployment": config["DEPLOYMENT"],
            "checked_at": now.isoformat(),
            "reminders": reminders,
        },
        indent=2,
    )
    publish(config, f"REMINDER: {config['DEPLOYMENT']} manual updates", body)
    return {"reminders": reminders, "last_sent": now.isoformat()}, True


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
    parser.add_argument(
        "--deployment-env-file",
        type=Path,
        default=Path("/etc/election-pipeline/deployment.env"),
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()

    os.umask(0o077)
    prod = dict(dotenv_values(args.env_file, interpolate=False))
    # CATALOG_FILE and CURRENT_RELEASE come from the deployment; alert settings from monitor.env.
    config = {
        **dotenv_values(args.deployment_env_file, interpolate=False),
        **dotenv_values(args.monitor_env_file, interpolate=False),
    }
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
            found = assess(prod, config, now)
            # Issues quote run errors; no secret leaves in an alert.
            for key, text in found.items():
                for secret in secrets:
                    text = text.replace(secret, "[redacted]")
                found[key] = text
            issues, reminders = split(found)
            result = {
                "outcome": "unhealthy" if issues else "healthy",
                "issues": issues,
                "reminders": reminders,
                "checked_at": now.isoformat(),
            }

            if not args.dry_run:
                STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = STATE_DIR / "state.json"
                previous = read_json(path) if path.exists() else {}
                updated, notified = notify_changes(config, issues, previous, now)
                atomic_json(path, updated)
                path = STATE_DIR / "reminders.json"
                previous = read_json(path) if path.exists() else {}
                updated, reminded = notify_reminders(config, reminders, previous, now)
                atomic_json(path, updated)
                result["reminded"] = reminded
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
