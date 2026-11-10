"""One source, one pinned snapshot, one recoverable collection-to-publication run."""

from __future__ import annotations

import json
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

NO_SNAPSHOT = "No archived snapshot to check"


def refused_by_worker(store, digest):
    """Whether the worker's latest run already failed to parse this snapshot."""
    try:
        latest = json.loads(
            (store.root / "runs" / store.source.source_id / "latest.json").read_text("utf-8")
        )
    except (OSError, ValueError):
        return False
    return latest.get("raw_sha256") == digest and any(
        s.get("stage") == "parse" and s.get("outcome") == "validation_failed"
        for s in latest.get("stages", [])
    )


def published_snapshot(source, env_file):
    """The raw snapshot the source currently publishes, if it came from this address."""
    from election_pipeline.database import connect, connection_settings

    with connect(connection_settings(env_file)) as conn:
        row = conn.execute(
            "SELECT p.raw_sha256 FROM serving.current_publications c "
            "JOIN serving.publications p USING (publication_id) "
            "WHERE c.source_id=%s AND p.source_url=%s",
            (source.source_id, source.url),
        ).fetchone()
    return row["raw_sha256"] if row else None


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
    check_only=False,
    attested=None,
):
    """Retry committed stages idempotently. Replay does not publish by default.

    ``attested`` is an operator's certification of an official publication (see
    ``publication.attestation``).

    ``prepare_only`` keeps a validated candidate for a later promotion. ``check_only``
    validates and discards one (a new release proving it can publish each source). It
    replays the source's latest archived snapshot, so it makes no request and leaves the
    collector state and latest run, which collection and monitoring read, as they were.
    """
    if status not in ("test", "unofficial", "official"):
        raise ValueError("Choose test, unofficial or official explicitly")

    if publish_replay and not replay_sha256:
        raise ValueError("--publish-replay requires --replay-sha256")
    if (prepare_only or check_only) and publish_replay:
        raise ValueError("Preparing or checking cannot publish a replay")

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
    path = (directory / "checks" if check_only else directory) / f"{report['run_id']}.json"
    report["report_path"] = str(path)

    started = time.monotonic()
    db = None

    def save():
        atomic_json(path, report)
        if not check_only:
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

                if check_only and not digest:
                    state = store.read_state()
                    # A snapshot of the address the source used before it moved would
                    # check the old feed's format, not the configured one.
                    if state.get("url") != source.url or not store.cached_archive(state):
                        raise ValueError(NO_SNAPSHOT)
                    digest = state["sha256"]

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

                if (
                    check_only
                    and not replay_sha256
                    and parsed["outcome"] == "validation_failed"
                    and refused_by_worker(store, digest)
                ):
                    # The state changed its format and the running release refuses its
                    # newest file too: no regression of this release. Check the snapshot
                    # the source publishes instead, so one state cannot block a release.
                    fallback = published_snapshot(source, env_file)
                    if fallback and fallback != digest and store.archive_path(fallback).is_file():
                        report["refused_snapshot"] = {
                            "raw_sha256": digest,
                            "error": parsed.get("error"),
                        }
                        digest = report["raw_sha256"] = fallback
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

                if prepare_only or check_only:
                    from election_pipeline.publication import discard, prepare

                    candidate = stage(
                        "validate_candidate",
                        lambda: prepare(
                            Path(root), db, loaded["release_id"], status, attested=attested
                        ),
                    )
                    report["candidate_id"] = candidate["candidate_id"]
                    if check_only:
                        stage("discard_candidate", lambda: discard(db, candidate["candidate_id"]))
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
                        attested=attested,
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
