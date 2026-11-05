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
    contract = ms.validate_source(source)
    county = next((c for c in contract["counties"] if c["key"] == county_key), None)
    if not county or county.get("url"):
        raise ValueError(
            "Select a reviewed-file county; automatic-source changes require a release"
        )
    return contract, county


def write_template(source, county_key, file, output):
    contract, _ = selected(source, county_key)
    if Path(file).stat().st_size > 8_000_000:
        raise ValueError("Report exceeds file limit")
    review = dict(
        state=source.state,
        county=county_key,
        election_date=source.election_date,
        contest_id=contract["contest_id"],
        raw_sha256=file_hash(Path(file)),
        source_url=None,
        official_page_url=None,
        observed_at=None,
        reviewed_by=None,
        countywide_report_confirmed=False,
        report_generated_at=None,
        report_date=None,
        report_time_note=None,
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
    if Path(file).stat().st_size > 8_000_000 or Path(review_file).stat().st_size > 100_000:
        raise ValueError("Report/review exceeds limit")
    raw = Path(file).read_bytes()
    review = json.loads(Path(review_file).read_text(encoding="utf-8-sig"))
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
                )
                if any(prior[1]["review"].get(k) != review.get(k) for k in semantic):
                    raise ValueError(
                        "The same file has a different review; resolve the discrepancy"
                    )
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
            issue="ManualSnapshot",
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
                requested_url=review["source_url"],
                sha256=fetched.sha256,
                archive_path=store.relative(archive),
                archive_created=created,
                acquisition_method="county_manual",
                source_generated_at=parsed["report_generated_at"],
                county=county_key,
                original_sha256=review["raw_sha256"],
                observed_at=review["observed_at"],
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
            return dict(
                outcome="manual_imported",
                source_id=source.source_id,
                county=county_key,
                sha256=fetched.sha256,
                archive_path=store.relative(archive),
                publication="pending normal validation/publication",
            )
        finally:
            temporary.unlink(missing_ok=True)
