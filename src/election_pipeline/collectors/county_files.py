"""Collect independent county files into one replayable state bundle.

State parsers provide the county contract and admission check. Original accepted
bytes stay in the bundle; rejected bytes go to quarantine. A county failure never
refreshes its last-success time or discards another county's accepted results.
"""

import hashlib
import json
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Lock

from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file
from election_pipeline.states import state_adapter
from election_pipeline.storage import file_hash, utc_now


class CollectionDeferred(ValueError):
    """Retain queue priority when no HTTP attempt fits this cycle."""


def fetch_bundle(
    source,
    settings,
    destination,
    *,
    previous=None,
    store=None,
    force_download=False,
    imports=None,
    offline=False,
):
    settings = source.effective_settings(settings)
    adapter = state_adapter(source.state)
    parser = adapter.parser
    contract = parser.validate_source(source)
    if previous is None and store is not None:
        predecessor = contract.get("previous_source_id")
        if predecessor and predecessor != source.source_id:
            from election_pipeline.storage import ArchiveStore

            legacy = ArchiveStore(store.root, replace(source, source_id=predecessor))
            try:
                previous = legacy.cached_archive(legacy.read_state())
            except (ValueError, OSError):
                previous = None
    old = {}
    scheduling = {}
    if previous is not None:
        # Revalidate cached bytes under the current parser before they can be fallback data.
        try:
            old = parser.previous_counties(previous, source)
            with zipfile.ZipFile(previous) as archive:
                scheduling = json.loads(archive.read(source.primary_filename))["counties"]
        except (ValueError, OSError, KeyError, zipfile.BadZipFile):
            old = {}
    old.update(imports or {})
    configured = [c for c in contract["counties"] if c.get("url")]
    configured.sort(
        key=lambda c: scheduling.get(c["key"], {}).get("retry_priority_at")
        or scheduling.get(c["key"], {}).get("last_attempt_at")
        or "1970"
    )
    budget_lock, budget = Lock(), [min(32 * 1024 * 1024, settings.max_download_bytes)]
    if not configured:
        raise FetchFailure("No county feeds configured", retryable=False)
    started = time.monotonic()
    components, paths = [], {}
    records = {
        c["key"]: {
            "status": "not_collected",
            "url": None,
            "raw_sha256": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "issue": None,
            "headers": {},
        }
        for c in contract["counties"]
    }
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as directory:
        directory = Path(directory)

        for county in contract["counties"]:
            key = county["key"]
            path = directory / county["member"]
            cached = old.get(key)
            if cached and (offline or not county.get("url")):
                records[key] = dict(cached[1])
                path.write_bytes(cached[3])
            else:
                path.write_bytes(b"")
                if offline and county.get("url"):
                    records[key].update(
                        status="unavailable",
                        url=county["url"],
                        last_attempt_at=utc_now(),
                        issue="NotChecked: offline county import",
                    )
            paths[path.name] = path

        def county_fetch(county):
            key = county["key"]
            cached = old.get(key)
            prior = cached[1] if cached else scheduling.get(key, {})
            conditions = {}
            # Validators belong to one URL; a manual snapshot has none for the new URL.
            if cached and not force_download and prior.get("url") == county["url"]:
                headers = prior.get("headers", {})
                if headers.get("etag"):
                    conditions["If-None-Match"] = headers["etag"]
                elif headers.get("last-modified"):
                    conditions["If-Modified-Since"] = headers["last-modified"]
            path = directory / ("fetch-" + county["member"])
            result, issue, rejected_sha, quarantine_path = None, None, None, None
            try:
                remaining = settings.max_download_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise CollectionDeferred("County collection cycle exhausted its time budget")
                with budget_lock:
                    allowance = min(8_000_000, budget[0])
                    if allowance <= 0:
                        raise CollectionDeferred("County cycle byte budget exhausted")
                    budget[0] -= allowance
                limits = replace(
                    settings,
                    timeout_seconds=min(12, settings.timeout_seconds, remaining),
                    max_download_seconds=min(15, remaining),
                    max_download_bytes=allowance,
                )
                try:
                    result = fetch_file(source, limits, path, conditions, url=county["url"])
                finally:
                    used = min(allowance, path.stat().st_size if path.exists() else 0)
                    with budget_lock:
                        budget[0] += allowance - used
                if result.status == 304:
                    if not cached or not conditions:
                        raise ValueError(
                            "County returned 304 without a validated conditional cache"
                        )
                    raw, parsed = cached[3], cached[2]
                elif result.status == 200:
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != result.sha256:
                        raise ValueError("County download fingerprint mismatch")
                    parsed = parser.read_county(raw, county, contract)
                    if (
                        cached
                        and cached[2].get("report_generated_at")
                        and parsed.get("report_generated_at")
                        and parser.stamp(parsed["report_generated_at"])
                        < parser.stamp(cached[2]["report_generated_at"])
                    ):
                        raise ValueError("County report generation time regressed")
                    if parsed.get("report_generated_at") and (
                        parser.stamp(parsed["report_generated_at"])
                        > parser.stamp(utc_now()) + timedelta(minutes=5)
                    ):
                        raise ValueError("County report generation time is in the future")
                else:
                    raise ValueError(f"County HTTP {result.status}")
                finished = utc_now()
                record = {
                    "status": "current",
                    "url": county["url"],
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "last_attempt_at": finished,
                    "last_success_at": finished,
                    "issue": None,
                    "headers": {
                        **(prior.get("headers", {}) if result.status == 304 else {}),
                        **{
                            k: v
                            for k, v in result.headers.items()
                            if k in {"etag", "last-modified"}
                        },
                    },
                }
            except (ValueError, OSError, FetchFailure, UnicodeError) as error:
                issue = (type(error).__name__ + ": " + str(error))[:700]
                if path.exists() and path.stat().st_size:
                    rejected_sha = file_hash(path)
                    if store is not None:
                        quarantine_path = store.relative(store.quarantine(path, rejected_sha))
                finished = utc_now()
                raw = cached[3] if cached else b""
                # Fallback keeps the identity of the bytes it carries. A reviewed import
                # of a newly automatic county keeps its URL, review and manual label.
                manual = bool(cached and prior.get("review"))
                record = {
                    "status": "stale" if cached else "unavailable",
                    "url": prior.get("url", county["url"]) if cached else county["url"],
                    "raw_sha256": hashlib.sha256(raw).hexdigest() if cached else None,
                    "last_attempt_at": finished,
                    "last_success_at": prior.get("last_success_at") if cached else None,
                    "issue": "ManualSnapshot" if manual else issue,
                    "headers": prior.get("headers", {}) if cached else {},
                }
                if manual:
                    record.update(review=prior["review"], automatic_issue=issue)
            if issue and issue.startswith("CollectionDeferred:"):
                record["retry_priority_at"] = (
                    prior.get("retry_priority_at") or prior.get("last_attempt_at") or "1970"
                )
            record["rejected_sha256"] = rejected_sha
            record["quarantine_path"] = quarantine_path
            accepted_path = directory / county["member"]
            accepted_path.write_bytes(raw)
            component = {
                "name": county["member"],
                "county": key,
                "url": county["url"],
                "final_url": result.final_url if result else county["url"],
                "http_status": result.status if result else None,
                "sha256": record["raw_sha256"],
                "bytes": result.bytes_received if result else 0,
                "collection_status": record["status"],
                "error": issue,
                "request_conditions": conditions,
                "rejected_sha256": rejected_sha,
                "quarantine_path": quarantine_path,
            }
            return key, record, accepted_path, component

        with ThreadPoolExecutor(max_workers=min(3, len(configured))) as pool:
            futures = (
                [] if offline else [pool.submit(county_fetch, county) for county in configured]
            )
            for future in as_completed(futures):
                key, record, path, component = future.result()
                records[key], paths[path.name] = record, path
                components.append(component)
        if not any(r["status"] in {"current", "stale"} for r in records.values()):
            raise FetchFailure(
                "No validated county reports; inspect quarantined files and source accessibility",
                retryable=False,
            )
        manifest = {
            "version": 2,
            "state": source.state,
            "election_date": source.election_date,
            "source_id": source.source_id,
            "policy_sha256": parser.POLICY_SHA256,
            "checked_at": utc_now(),
            "counties": records,
        }
        manifest_path = directory / source.primary_filename
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        paths[source.primary_filename] = manifest_path
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(paths.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return FetchResponse(
        200,
        {"content-type": "application/zip"},
        source.url,
        sum(c["bytes"] for c in components),
        file_hash(destination),
        tuple(sorted(components, key=lambda c: c["name"])),
    )
