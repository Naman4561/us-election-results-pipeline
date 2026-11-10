"""Import an operator-downloaded, single-file official feed without making HTTP requests."""

import hashlib
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from election_pipeline import __version__
from election_pipeline.checks import validate_payload
from election_pipeline.parsers import certified as certified_files
from election_pipeline.parsing import parse_source
from election_pipeline.pipeline import collector_code_hash
from election_pipeline.storage import ArchiveStore, file_hash, utc_now


def import_file(
    source,
    settings,
    data_dir,
    path,
    downloaded_at,
    expected_sha256,
    *,
    minimum=None,
    certified=None,
):
    """Validate offline, then commit an immutable archive and an honest acquisition receipt.

    ``certified`` names a certified file (``certified-<format>``, see
    ``parsers.certified``): the archive then holds the state's certified results under
    that name instead of the source's own feed file, for any results source.
    """
    if source.dataset != "election_results":
        raise ValueError("Manual import accepts election results only")
    if certified is None and (source.format != "bundle" or source.files):
        raise ValueError("Manual import currently supports single-file result bundles only")
    if certified is not None and not certified.startswith(certified_files.PREFIX):
        raise ValueError("A certified file's name starts with " + certified_files.PREFIX)
    observed = datetime.fromisoformat(downloaded_at)
    if observed.tzinfo is None or not (
        datetime.fromisoformat(source.election_date).replace(tzinfo=timezone.utc)
        <= observed
        <= datetime.now(timezone.utc) + timedelta(seconds=60)
    ):
        raise ValueError(
            "Download time must include a timezone and be between election day and now"
        )
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError("Supply the original file SHA-256 from the downloading computer")
    path = Path(path)
    with path.open("rb") as handle:
        raw = handle.read(settings.max_download_bytes + 1)
    if not raw or len(raw) > settings.max_download_bytes:
        raise ValueError("The local file is empty or exceeds the source size budget")
    original_sha = hashlib.sha256(raw).hexdigest()
    if original_sha != expected_sha256.lower():
        raise ValueError("The uploaded file does not match the original SHA-256")
    store = ArchiveStore(Path(data_dir), source)
    store.root.mkdir(parents=True, exist_ok=True)
    # Rejected HTML, wrong elections, missing counties and inconsistent totals never
    # enter the canonical archive store or the database.
    with tempfile.TemporaryDirectory(prefix=".manual-check-", dir=store.root) as temporary:
        staged = ArchiveStore(Path(temporary), source)
        bundle = staged.temporary_download()
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # A feed's archive is the same for the same bytes. A certified file is dated
            # by its download (UTC), the time a state's reader may record as its check.
            stamp = observed.astimezone(timezone.utc).timetuple()[:6] if certified else None
            info = zipfile.ZipInfo(
                certified or source.primary_filename, date_time=stamp or (1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, raw)
        if certified is None:
            validate_payload(bundle, source, settings)
        digest = file_hash(bundle)
        staged.commit_download(bundle, digest)
        checked = parse_source(source, settings, staged.root, sha256=digest)
        if checked.get("outcome") != "parsed":
            raise ValueError("Manual file rejected: " + str(checked.get("error", checked)))
        # Ohio's XML prints an ISO timestamp. Adapters whose source text is not ISO
        # (Kansas) supply source_generated_at; others cannot be imported manually yet.
        # A certified file is dated by its certification, recorded in the attestation.
        generated = checked.get("source_generated_at") or checked.get("source_update_text")
        generated_at = None
        if certified is None or generated:
            try:
                generated_at = datetime.fromisoformat(generated)
            except (ValueError, TypeError) as error:
                raise ValueError(
                    "Manual import requires an ISO report generation timestamp"
                ) from error
            if generated_at.tzinfo is None or generated_at > observed + timedelta(seconds=60):
                raise ValueError(
                    "The report generation time is missing a timezone or follows download"
                )
        if minimum and generated_at and generated_at < datetime.fromisoformat(minimum):
            raise ValueError("This report is older than the last published manual snapshot")
        receipt = {
            "schema_version": 1,
            "run_id": uuid.uuid4().hex,
            "fetch_id": uuid.uuid4().hex,
            "source_id": source.source_id,
            "state": source.state,
            "election_date": source.election_date,
            "dataset": source.dataset,
            "requested_url": source.url,
            "source_signature": source.signature(settings),
            "collector_version": __version__,
            "collector_code_sha256": collector_code_hash(),
            "verification_level": "file_structure_only",
            "outcome": "manual_imported",
            "acquisition_method": "manual",
            "http_status": None,
            "attempt": 0,
            "started_at": observed.isoformat(),
            "finished_at": observed.isoformat(),
            "downloaded_at": observed.isoformat(),
            "imported_at": utc_now(),
            "source_generated_at": generated_at.isoformat() if generated_at else None,
            "certified_file": certified,
            "original_filename": path.name,
            "original_sha256": original_sha,
            "original_bytes": len(raw),
            "sha256": digest,
        }
        with store.lock():
            store.raw_dir.mkdir(parents=True, exist_ok=True)
            archive_path, created = store.commit_download(staged.archive_path(digest), digest)
            receipt.update(archive_path=store.relative(archive_path), archive_created=created)
            metadata = store.write_attempt(receipt)
        return {**receipt, "metadata_path": str(metadata)}
