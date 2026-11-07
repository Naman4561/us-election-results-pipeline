"""One source, one pinned snapshot, one recoverable collection-to-publication run."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from election_pipeline.parsing import parse_source
from election_pipeline.pipeline import collect_source
from election_pipeline.storage import (
    ArchiveStore,
    CollectionLocked,
    atomic_json,
    utc_now,
)


def run_source(
    source,
    settings,
    data_dir,
    *,
    root,
    env_file=Path(".env"),
    status,
    replay_sha256=None,
    publish_replay=False,
    reference=None,
    prepare_only=False,
):
    """Retry committed stages idempotently. Replay does not publish by default."""
    if status not in ("test", "unofficial"):
        raise ValueError("Choose test or unofficial explicitly")

    if publish_replay and not replay_sha256:
        raise ValueError("--publish-replay requires --replay-sha256")
    if prepare_only and publish_replay:
        raise ValueError("--prepare-only cannot publish a replay")

    store = ArchiveStore(Path(data_dir), source)

    if replay_sha256:
        store.archive_path(replay_sha256)

    report = {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "source_id": source.source_id,
        "mode": "replay" if replay_sha256 else "live",
        "status": status,
        "started_at": utc_now(),
        "outcome": "running",
        "stages": [],
    }

    directory = store.root / "runs" / source.source_id
    path = directory / f"{report['run_id']}.json"
    report["report_path"] = str(path)

    started = time.monotonic()
    db = None

    def save():
        atomic_json(path, report)
        atomic_json(directory / "latest.json", report)

    def stage(name, function):
        item = {
            "stage": name,
            "started_at": utc_now(),
            "outcome": "running",
        }
        report["stages"].append(item)
        save()

        clock = time.monotonic()

        try:
            result = function()
            item["result"] = result
            item["outcome"] = result.get("outcome", "succeeded")
            return result
        except KeyboardInterrupt:
            item["outcome"] = "interrupted"
            raise
        except Exception:
            item["outcome"] = "failed"
            raise
        finally:
            item["finished_at"] = utc_now()
            item["duration_seconds"] = round(time.monotonic() - clock, 3)
            save()

    try:
        with store.lock(stage="pipeline"):
            save()

            try:
                digest = replay_sha256

                if not digest:
                    fetched = stage(
                        "collect",
                        lambda: collect_source(source, settings, store.root),
                    )
                    outcome = fetched["outcome"]

                    if outcome in ("not_due", "deferred", "backing_off"):
                        state = store.read_state()

                        if (
                            state.get("source_signature") == source.signature(settings)
                            and state.get("url") == source.url
                            and store.cached_archive(state)
                        ):
                            digest = state["sha256"]
                        else:
                            report["outcome"] = outcome
                            return report

                    elif outcome in ("downloaded", "unchanged"):
                        digest = fetched["sha256"]

                    elif outcome == "awaiting_publication":
                        report["outcome"] = outcome
                        return report

                    else:
                        raise ValueError(f"Collection stopped: {outcome}; inspect fetch metadata")

                report["raw_sha256"] = digest

                parsed = stage(
                    "parse",
                    lambda: parse_source(
                        source,
                        settings,
                        store.root,
                        sha256=digest,
                        reference=reference,
                    ),
                )

                if parsed["outcome"] not in ("parsed", "already_parsed"):
                    raise ValueError(f"Parsing failed: {parsed.get('error', parsed['outcome'])}")

                if parsed.get("internal_reconciliation") != "passed":
                    raise ValueError("Parser reconciliation did not pass")

                if parsed.get("reference_comparison", {}).get("status") == "mismatch":
                    raise ValueError("Independent reference mismatch; load/publication blocked")

                # Capture the raw snapshot before requiring database availability.
                def load():
                    nonlocal db

                    from election_pipeline.database import (
                        connection_settings,
                        load_parsed,
                    )

                    db = connection_settings(env_file)

                    return load_parsed(
                        source,
                        settings,
                        store.root,
                        Path(parsed["output_dir"]),
                        db,
                    )

                loaded = stage("load", load)

                if loaded["outcome"] not in ("succeeded", "skipped"):
                    raise ValueError(f"Load failed: {loaded.get('error', loaded['outcome'])}")

                report["release_id"] = loaded["release_id"]

                if prepare_only:
                    from election_pipeline.publication import prepare

                    candidate = stage(
                        "validate_candidate",
                        lambda: prepare(Path(root), db, loaded["release_id"], status),
                    )
                    report["candidate_id"] = candidate["candidate_id"]
                    report["outcome"] = "validated"
                    return report

                if replay_sha256 and not publish_replay:
                    report["outcome"] = "replayed_not_published"
                    return report

                from election_pipeline.publication import publish

                published = stage(
                    "validate_and_publish",
                    lambda: publish(
                        Path(root),
                        db,
                        loaded["release_id"],
                        status,
                        skip_current=True,
                    ),
                )

                report["publication_id"] = published["publication_id"]
                report["outcome"] = (
                    "already_current" if published["outcome"] == "already_current" else "succeeded"
                )

            except KeyboardInterrupt:
                report["outcome"] = "interrupted"
                raise

            except Exception as error:
                report["outcome"] = "failed"
                report["error"] = (
                    str(error).replace(db["password"], "[redacted]") if db else str(error)
                )

            finally:
                report["finished_at"] = utc_now()
                report["duration_seconds"] = round(time.monotonic() - started, 3)
                save()

    except CollectionLocked:
        # Preserve the active owner's latest.json.
        report["outcome"] = "locked"
        report["finished_at"] = utc_now()
        atomic_json(path, report)

    return report
