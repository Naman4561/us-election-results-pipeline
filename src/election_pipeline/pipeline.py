"""Collect a source, record each attempt, and advance HTTP cache state only after validation."""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from election_pipeline import __version__
from election_pipeline.checks import InvalidPayload, validate_payload
from election_pipeline.collect import FetchFailure, fetch_file
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.storage import ArchiveStore, CollectionLocked, utc_now

RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# Access refusals: the site (or its firewall) is declining this client.
BLOCKED_STATUSES = {401, 403, 451}
BACKOFF_BASE_SECONDS = 300
BACKOFF_MAX_SECONDS = 3600
BLOCKED_BACKOFF_MAX_SECONDS = 6 * 3600
FAILURE_KEYS = ("consecutive_failures", "failure_backoff_until", "blocked_since", "last_failure")


def failure_backoff(failures: int, blocked: bool) -> float:
    """Seconds to wait after the nth consecutive failed collection run.

    5, 15, 45 minutes, then hourly; a site refusing access backs off to six hours, so a
    firewall or challenge is not retried every scheduler tick, yet recovery needs no
    operator once the site allows the client again.
    """
    cap = BLOCKED_BACKOFF_MAX_SECONDS if blocked else BACKOFF_MAX_SECONDS
    return float(min(cap, BACKOFF_BASE_SECONDS * 3 ** min(failures - 1, 10)))


def collector_code_hash(package_dir: Path | None = None) -> str:
    """Identify the shipped Python sources and reviewed parser data, without needing Git."""
    digest = hashlib.sha256()
    package_dir = package_dir if package_dir is not None else Path(__file__).parent
    # Reviewed data the parsers check against (rosters, county lists) counts as code.
    sources = [*package_dir.rglob("*.py"), *(package_dir / "resources").rglob("*.json")]
    for path in sorted(sources):
        name = path.relative_to(package_dir).as_posix()
        digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _summary(record: dict, metadata: Path | None = None) -> dict:
    keys = (
        "source_id",
        "run_id",
        "outcome",
        "attempt",
        "http_status",
        "sha256",
        "archive_path",
        "archive_created",
        "changed",
        "error",
        "retry_not_before",
        "verification_level",
        "dataset",
        "next_check_at",
        "consecutive_failures",
    )
    result = {key: record[key] for key in keys if key in record}
    if metadata is not None:
        result["metadata_path"] = str(metadata)
    return result


def _record_failure(store: ArchiveStore, state: dict, record: dict) -> dict:
    """Persist one more consecutive failed run and when the next request may happen."""
    failures = int(state.get("consecutive_failures") or 0) + 1
    blocked = record.get("http_status") in BLOCKED_STATUSES
    now = datetime.now(timezone.utc)
    until = (now + timedelta(seconds=failure_backoff(failures, blocked))).isoformat(
        timespec="seconds"
    )
    store.write_state(
        {
            **state,
            "consecutive_failures": failures,
            "failure_backoff_until": until,
            "blocked_since": (state.get("blocked_since") or now.isoformat(timespec="seconds"))
            if blocked
            else None,
            "last_failure": record.get("error") or record.get("outcome"),
        }
    )
    return {"consecutive_failures": failures, "retry_not_before": until}


def collect_source(
    source: Source,
    settings: CollectorSettings,
    data_dir: Path,
    *,
    force_download: bool = False,
) -> dict:
    store = ArchiveStore(data_dir, source)
    run_id = uuid.uuid4().hex
    try:
        with store.lock():
            return _collect_locked(store, source, settings, run_id, force_download)
    except CollectionLocked:
        return {"source_id": source.source_id, "run_id": run_id, "outcome": "locked"}


def _collect_locked(
    store: ArchiveStore,
    source: Source,
    settings: CollectorSettings,
    run_id: str,
    force_download: bool,
) -> dict:
    settings = source.effective_settings(settings)
    signature = source.signature(settings)
    state = store.read_state()
    from election_pipeline.states import STATES

    adapter = STATES.get(source.state)
    previous_counties = (
        store.cached_archive(state)
        if adapter and adapter.collection_backend == "county_files"
        else None
    )
    if state.get("source_signature") != signature:
        # A new release or source setting. For the same URL the last archived snapshot and
        # the failure record still hold, so a site that is down keeps its fallback; only
        # the HTTP validators belong to the old request.
        kept = state if state.get("url") == source.url else {}
        state = {
            **{k: v for k, v in kept.items() if k not in ("etag", "last_modified")},
            "source_signature": signature,
            "url": source.url,
        }
        if kept:
            store.write_state(state)
    store.remove_abandoned_downloads()
    base = {
        "schema_version": 1,
        "run_id": run_id,
        "source_id": source.source_id,
        "state": source.state,
        "election_date": source.election_date,
        "requested_url": source.url,
        "source_signature": signature,
        "collector_version": __version__,
        "collector_code_sha256": collector_code_hash(),
        "verification_level": ("archive_only" if source.format == "zip" else "file_structure_only"),
        "dataset": source.dataset,
    }
    not_before = state.get("retry_not_before")
    if not_before and datetime.fromisoformat(not_before) > datetime.now(timezone.utc):
        record = {
            **base,
            "fetch_id": uuid.uuid4().hex,
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "attempt": 0,
            "outcome": "deferred",
            "http_status": None,
            "retry_not_before": not_before,
        }
        return _summary(record, store.write_attempt(record))

    backoff_until = state.get("failure_backoff_until")
    if (
        backoff_until
        and not force_download
        and datetime.fromisoformat(backoff_until) > datetime.now(timezone.utc)
    ):
        # Nothing is requested, so nothing is recorded per tick; the state file and the
        # last failed attempt already explain the wait.
        return {
            "source_id": source.source_id,
            "run_id": run_id,
            "outcome": "backing_off",
            "attempt": 0,
            "retry_not_before": backoff_until,
            "consecutive_failures": state.get("consecutive_failures"),
            "error": state.get("last_failure"),
        }

    cached = store.cached_archive(state)
    if (
        source.min_interval_seconds
        and not force_download
        and state.get("last_checked_at")
        and (cached is not None or state.get("availability") == "awaiting_publication")
    ):
        next_check = datetime.fromisoformat(state["last_checked_at"]) + timedelta(
            seconds=source.min_interval_seconds
        )

        if datetime.now(timezone.utc) < next_check:
            # No request is made, so no fetch record is written: one per scheduler tick
            # would grow without bound and say nothing the state file does not.
            return {
                "source_id": source.source_id,
                "run_id": run_id,
                "outcome": "not_due",
                "attempt": 0,
                "next_check_at": next_check.isoformat(),
            }
    use_conditions = cached is not None and not force_download and source.format != "bundle"
    for attempt in range(1, settings.max_attempts + 1):
        conditions = {}
        if use_conditions:
            if state.get("etag"):
                conditions["If-None-Match"] = state["etag"]
            elif state.get("last_modified"):
                conditions["If-Modified-Since"] = state["last_modified"]
        record = {
            **base,
            "fetch_id": uuid.uuid4().hex,
            "started_at": utc_now(),
            "attempt": attempt,
            "outcome": "in_progress",
            "http_status": None,
            "request_conditions": conditions,
        }
        metadata_path = store.write_attempt(record)
        temporary = store.temporary_download()
        started = time.monotonic()
        retryable = False
        server_wait = None
        new_state = None
        try:
            if source.format == "bundle":
                from election_pipeline.bundles import fetch_bundle
                from election_pipeline.states import STATES

                adapter = STATES.get(source.state)
                if adapter and adapter.collection_backend:
                    fetched = fetch_bundle(
                        source,
                        settings,
                        temporary,
                        previous=cached or previous_counties,
                        store=store,
                        force_download=force_download,
                    )
                else:
                    fetched = fetch_bundle(source, settings, temporary)
            else:
                fetched = fetch_file(source, settings, temporary, conditions)
            record.update(
                {
                    "http_status": fetched.status,
                    "response_headers": fetched.headers,
                    "final_url": fetched.final_url,
                    "bytes_received": fetched.bytes_received,
                    "components": list(fetched.components),
                }
            )
            if fetched.status == 304:
                if cached is None or not use_conditions:
                    record.update(outcome="unexpected_304", error="No usable conditional cache")
                    use_conditions = False
                    retryable = True
                else:
                    record.update(
                        {
                            "outcome": "unchanged",
                            "changed": False,
                            "archive_created": False,
                            "sha256": state["sha256"],
                            "archive_path": store.relative(cached),
                        }
                    )
                    new_state = {**state, "last_checked_at": utc_now()}
                    for header, key in (("etag", "etag"), ("last-modified", "last_modified")):
                        if header in fetched.headers:
                            new_state[key] = fetched.headers[header]
            elif fetched.status == 200:
                record["sha256"] = fetched.sha256
                try:
                    record.update(validate_payload(temporary, source, settings))
                except InvalidPayload as error:
                    rejected = store.quarantine(temporary, fetched.sha256)
                    record.update(
                        {
                            "outcome": (
                                "invalid_archive" if source.format == "zip" else "invalid_payload"
                            ),
                            "error": str(error),
                            "quarantine_path": store.relative(rejected),
                        }
                    )
                else:
                    path, created = store.commit_download(temporary, fetched.sha256)
                    changed = state.get("sha256") != fetched.sha256
                    record.update(
                        {
                            "outcome": "downloaded" if changed else "unchanged",
                            "changed": changed,
                            "archive_created": created,
                            "archive_path": store.relative(path),
                        }
                    )
                    previous_modified = state.get("last_modified")
                    current_modified = fetched.headers.get("last-modified")
                    if previous_modified and current_modified:
                        try:
                            record["source_time_regressed"] = parsedate_to_datetime(
                                current_modified
                            ) < parsedate_to_datetime(previous_modified)
                        except (ValueError, TypeError):
                            record["source_time_regressed"] = None
                    new_state = {
                        "source_signature": signature,
                        "url": source.url,
                        "sha256": fetched.sha256,
                        "etag": fetched.headers.get("etag"),
                        "last_modified": fetched.headers.get("last-modified"),
                        "last_checked_at": utc_now(),
                        "last_changed_at": utc_now() if changed else state.get("last_changed_at"),
                        "availability": "available",
                    }
            elif fetched.status == 404 and source.allow_missing and not state.get("sha256"):
                # Expected only before this source's first valid download.
                # Disappearance of an established file remains an error.
                record.update(outcome="awaiting_publication")

                new_state = {
                    **state,
                    "last_checked_at": utc_now(),
                    "availability": "awaiting_publication",
                }
            else:
                record.update(outcome="http_error", error=f"HTTP {fetched.status}")
                retryable = fetched.status in RETRYABLE_STATUSES
                if retryable:
                    server_wait = retry_after_seconds(fetched.headers.get("retry-after"))
        except FetchFailure as error:
            record.update(
                {
                    "outcome": "network_error",
                    "error": str(error),
                    "http_status": error.status,
                    "response_headers": error.headers,
                    "bytes_received": error.bytes_received,
                }
            )
            retryable = error.retryable
        except (OSError, ValueError) as error:
            record.update(outcome="storage_or_validation_error", error=str(error))
        finally:
            temporary.unlink(missing_ok=True)
            record["finished_at"] = utc_now()
            record["elapsed_seconds"] = round(time.monotonic() - started, 6)

        if server_wait is not None:
            try:
                when = datetime.now(timezone.utc) + timedelta(seconds=server_wait)
            except (OverflowError, ValueError):
                when = datetime.max.replace(tzinfo=timezone.utc)
            not_before = when.isoformat(timespec="milliseconds")
            record["retry_not_before"] = not_before
            store.write_state({**state, "retry_not_before": not_before})
            if server_wait > settings.max_retry_wait_seconds:
                record["outcome"] = "deferred"
                record["error"] = "Retry-After exceeds this run's wait budget; retry later"
                retryable = False

        # Record the observed response before advancing validators. If either write fails,
        # the CLI exits nonzero. A later unconditional/old-validator fetch can recover.
        store.write_attempt(record)
        if new_state is not None:
            new_state.pop("retry_not_before", None)
            for key in FAILURE_KEYS:
                new_state.pop(key, None)
            store.write_state(new_state)
            return _summary(record, metadata_path)
        if retryable and attempt < settings.max_attempts:
            delay = min(
                settings.max_retry_wait_seconds,
                settings.backoff_seconds * 2 ** (attempt - 1)
                + random.uniform(0, settings.backoff_seconds),
            )
            if server_wait is not None:
                delay = max(delay, server_wait)
            time.sleep(delay)
            if record["outcome"] == "unexpected_304":
                cached = None
            continue
        if record["outcome"] != "deferred":
            record.update(_record_failure(store, state, record))
        return _summary(record, metadata_path)
    raise AssertionError("A bounded collection loop must return a result")
