"""Review-bound county imports; never make HTTP requests or publish to PostgreSQL."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from election_pipeline.checks import validate_payload
from election_pipeline.collectors import county_files
from election_pipeline.parsers import ms
from election_pipeline.parsers.county_reports import reviewed_file
from election_pipeline.pipeline import collector_code_hash
from election_pipeline.storage import ArchiveStore, file_hash, utc_now


def selected(source, county_key):
    """A reviewed-file county, or an automatic one whose feed has never been accepted (for
    example, a county site that refuses the server). Its reviewed copy must be the report
    at its configured URL, and polling continues: an accepted automatic response replaces
    the manual snapshot.

    Any county may instead take its certified Official Recapitulation (a review with
    ``certified: true``), from wherever it is posted. That is its final count: it replaces
    an automatic feed's results, the county is no longer polled, and only another certified
    recap can replace it."""
    contract = ms.validate_source(source)
    county = next((c for c in contract["counties"] if c["key"] == county_key), None)
    if not county:
        raise ValueError("Select a configured Mississippi county")
    return contract, county


def write_template(source, county_key, file, output):
    contract, county = selected(source, county_key)
    if Path(file).stat().st_size > ms.CERTIFIED_BYTES:
        raise ValueError("Report exceeds file limit")
    review = dict(
        state=source.state,
        county=county_key,
        election_date=source.election_date,
        contest_id=contract["contest_id"],
        raw_sha256=file_hash(Path(file)),
        source_url=county.get("url"),
        official_page_url=None,
        observed_at=None,
        reviewed_by=None,
        countywide_report_confirmed=False,
        report_generated_at=None,
        report_date=None,
        report_time_note=None,
        certified=False,
        certified_on=None,
        counts={n: None for n in contract["candidates"]},
        write_in_votes=None,
        write_in_label=None,
        reported_candidate_vote_total=None,
        precincts_reporting=None,
        precincts_total=None,
        absentee_scope="unspecified",
        affidavit_scope="unspecified",
    )
    with Path(output).open("x", encoding="utf-8", newline="\n") as f:
        json.dump(review, f, indent=2)
        f.write("\n")
    return dict(outcome="template_created", county=county_key, path=str(Path(output).resolve()))


def import_county(source, settings, data_dir, county_key, file, review_file):
    contract, county = selected(source, county_key)
    if Path(review_file).stat().st_size > 100_000:
        raise ValueError("Report/review exceeds limit")
    review = json.loads(Path(review_file).read_text(encoding="utf-8-sig"))
    certified = isinstance(review, dict) and review.get("certified") is True
    if Path(file).stat().st_size > (ms.CERTIFIED_BYTES if certified else 8_000_000):
        raise ValueError("Report/review exceeds limit")
    raw = Path(file).read_bytes()
    parsed = reviewed_file(raw, county, contract, review)
    observed = ms.stamp(review["observed_at"])
    if observed > datetime.now(timezone.utc) + timedelta(minutes=5) or observed < ms.stamp(
        "2026-11-03T00:00:00-06:00"
    ):
        raise ValueError("Unsupported observation time")
    store = ArchiveStore(Path(data_dir), source)
    with store.lock():
        state = store.read_state()
        previous = store.cached_archive(state)
        if previous is None:
            raise ValueError("Collect the source once before importing county files")
        accepted = ms.previous_counties(previous, source)
        if not accepted:
            raise ValueError("No validated prior county bundle")
        prior = accepted.get(county_key)
        if prior and prior[1].get("issue") == ms.COUNTY_CERTIFIED and not certified:
            raise ValueError("Only another certified recap can replace a certified county")
        if county.get("url") and not certified:
            if review["source_url"] != county["url"]:
                raise ValueError("An automatic county's review must name its configured URL")
            if prior and not prior[1].get("review"):
                raise ValueError("The automatic county feed already has accepted results")
        if prior and certified and prior[1].get("issue") != ms.COUNTY_CERTIFIED:
            # The certified recap supersedes the earlier snapshot, whatever its times.
            prior = None
        if prior:
            if prior[1]["raw_sha256"] == review["raw_sha256"]:
                semantic = (
                    "counts",
                    "write_in_votes",
                    "write_in_label",
                    "reported_candidate_vote_total",
                    "precincts_reporting",
                    "precincts_total",
                    "report_generated_at",
                    "report_date",
                    "absentee_scope",
                    "affidavit_scope",
                    "source_url",
                    "official_page_url",
                    "report_time_note",
                    "certified",
                    "certified_on",
                    "house",
                )
                if any(prior[1]["review"].get(k) != review.get(k) for k in semantic):
                    raise ValueError(
                        "The same file has a different review; resolve the discrepancy"
                    )
                # A later check that found the same file confirms the snapshot is still
                # current: it refreshes the observation time, never the report time.
                if observed <= ms.stamp(prior[1]["last_success_at"]):
                    return dict(
                        outcome="already_imported",
                        source_id=source.source_id,
                        county=county_key,
                        sha256=state["sha256"],
                    )
            if observed < ms.stamp(prior[1]["last_success_at"]) or (
                parsed["report_generated_at"]
                and prior[2]["report_generated_at"]
                and ms.stamp(parsed["report_generated_at"])
                < ms.stamp(prior[2]["report_generated_at"])
            ):
                raise ValueError("Report/observation time regressed")
        record = dict(
            status="stale",
            url=review["source_url"],
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            last_attempt_at=review["observed_at"],
            last_success_at=review["observed_at"],
            issue=ms.COUNTY_CERTIFIED if certified else "ManualSnapshot",
            headers={},
            review=review,
        )
        temporary = store.temporary_download()
        try:
            fetched = county_files.fetch_bundle(
                source,
                settings,
                temporary,
                previous=previous,
                store=store,
                imports={county_key: (county, record, parsed, raw)},
                offline=True,
            )
            archive = commit_offline(
                store,
                state,
                source,
                settings,
                temporary,
                fetched,
                requested_url=review["source_url"],
                source_generated_at=parsed["report_generated_at"],
                county=county_key,
                original_sha256=review["raw_sha256"],
                observed_at=review["observed_at"],
            )
            confirmed = prior and prior[1]["raw_sha256"] == review["raw_sha256"]
            return dict(
                outcome="manual_confirmed" if confirmed else "manual_imported",
                source_id=source.source_id,
                county=county_key,
                sha256=fetched.sha256,
                archive_path=store.relative(archive),
                publication="pending normal validation/publication",
            )
        finally:
            temporary.unlink(missing_ok=True)


def commit_offline(store, state, source, settings, temporary, fetched, **details):
    """Validate and keep an offline rebuild of the county bundle, with its receipt."""
    validate_payload(temporary, source, settings)
    ms.read_bundle(temporary, source)
    archive, created = store.commit_download(temporary, fetched.sha256)
    now = utc_now()
    receipt = dict(
        schema_version=1,
        fetch_id=uuid.uuid4().hex,
        run_id=uuid.uuid4().hex,
        source_id=source.source_id,
        state=source.state,
        election_date=source.election_date,
        dataset=source.dataset,
        source_signature=source.signature(settings),
        collector_code_sha256=collector_code_hash(),
        verification_level="county_reviewed_original",
        started_at=now,
        finished_at=now,
        attempt=0,
        outcome="manual_imported",
        http_status=None,
        sha256=fetched.sha256,
        archive_path=store.relative(archive),
        archive_created=created,
        acquisition_method="county_manual",
        **details,
    )
    store.write_attempt(receipt)
    # No HTTP occurred: last_checked_at must not be refreshed.
    store.write_state(
        {
            **state,
            "source_signature": source.signature(settings),
            "url": source.url,
            "sha256": fetched.sha256,
            "last_changed_at": now,
            "availability": "available",
        }
    )
    return archive


def set_house(source, settings, data_dir, enabled, allow_missing=()):
    """Turn Mississippi's House races on (or off) in the county bundle.

    Turning them on requires a certified House count for every county but those named in
    ``allow_missing``, so each district is complete or knowingly partial. The next run
    publishes the districts; the setting is carried forward on every poll after that.
    """
    allow_missing = set(allow_missing)
    store = ArchiveStore(Path(data_dir), source)
    with store.lock():
        state = store.read_state()
        previous = store.cached_archive(state)
        if previous is None:
            raise ValueError("Collect the source once before changing its House setting")
        contract, manifest, accepted = ms.read_bundle(previous, source)
        if manifest.get("options", {}).get("house", False) is enabled:
            return dict(outcome="unchanged", source_id=source.source_id, house=enabled)
        if enabled:
            keys = {c["key"] for c in contract["counties"]}
            if not allow_missing <= keys:
                raise ValueError("Name configured Mississippi counties to allow missing")
            certified = {
                key
                for key, (_, record, parsed, _) in accepted.items()
                if record.get("issue") == ms.COUNTY_CERTIFIED and parsed.get("house")
            }
            missing = sorted(keys - certified - allow_missing)
            if missing:
                raise ValueError(
                    "These counties have no certified House count: " + ", ".join(missing)
                )
            districts = {contest for key in certified for contest in accepted[key][2]["house"]}
            if districts != set(ms.house_policy()["districts"]):
                raise ValueError("Every House district needs at least one certified county")
        temporary = store.temporary_download()
        try:
            fetched = county_files.fetch_bundle(
                source,
                settings,
                temporary,
                previous=previous,
                store=store,
                offline=True,
                options={**manifest.get("options", {}), "house": enabled},
            )
            archive = commit_offline(
                store,
                state,
                source,
                settings,
                temporary,
                fetched,
                requested_url=None,
                source_generated_at=None,
                county=None,
                original_sha256=None,
                observed_at=utc_now(),
            )
        finally:
            temporary.unlink(missing_ok=True)
    return dict(
        outcome="house_enabled" if enabled else "house_disabled",
        source_id=source.source_id,
        allowed_missing=sorted(allow_missing),
        sha256=fetched.sha256,
        archive_path=store.relative(archive),
        publication="pending normal validation/publication",
    )
