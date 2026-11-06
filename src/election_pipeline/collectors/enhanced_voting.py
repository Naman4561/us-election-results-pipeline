"""Official Enhanced Voting results sites (Virginia, Idaho, Washington).

The public results API serves one ``data`` document with every contest's statewide
totals, and one ``ballot-items/<id>`` document per contest with its county (locality)
breakdown. The collector GETs ``data`` (the configured source URL), asks the state's
parser which contests it needs, then GETs exactly those ballot items from the same
election path. These are the same documents the official results page loads; nothing is
posted, solved or guessed.

The state's parser module declares ``ballot_items(raw_data, source)``, which returns
``(member, ballot_item_id)`` for each selected contest.

If ``data`` is byte-identical to the previous bundle's (and that bundle is under
``REFRESH_SECONDS`` old) the previous bundle is reused. After the ballot items are
fetched, ``data`` is fetched once more: if it changed meanwhile, the site published new
results during collection, so the attempt fails (retryably) instead of pairing statewide
totals and county detail from two different publications.
"""

import hashlib
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file
from election_pipeline.collectors.enr_export import reusable
from election_pipeline.states import state_adapter

ITEM = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATA_PATH = re.compile(
    r"/results/public/api/elections/[A-Za-z0-9_-]{1,40}/[A-Za-z0-9_-]{1,80}/data"
)


def item_url(data_url, identity):
    if not ITEM.fullmatch(identity):
        raise ValueError("Unexpected ballot item identifier")
    parts = urlsplit(data_url)
    if parts.scheme != "https" or parts.query or not DATA_PATH.fullmatch(parts.path):
        raise ValueError("Source URL is not an Enhanced Voting election data document")
    return f"https://{parts.netloc}{parts.path[: -len('data')]}ballot-items/{identity}"


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    if source.files:
        raise FetchFailure("Enhanced Voting bundles take no reference files", retryable=False)
    components, received = [], 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as directory:
        paths = {}

        def get(name, url):
            nonlocal received
            remaining = settings.max_download_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise FetchFailure("Bundle exceeded its elapsed-time budget")
            if received >= settings.max_download_bytes:
                raise FetchFailure("Bundle exceeds its combined download budget", retryable=False)
            limits = replace(
                settings,
                max_download_seconds=remaining,
                timeout_seconds=min(settings.timeout_seconds, remaining),
                max_download_bytes=settings.max_download_bytes - received,
            )
            path = Path(directory) / name
            result = fetch_file(source, limits, path, {}, url=url)
            if result.status != 200:
                return result
            received += result.bytes_received
            if not result.headers.get("content-type", "").startswith("application/json"):
                raise FetchFailure(f"Official results API did not return JSON for {name}")
            paths[name] = path
            components.append(
                {
                    "name": name,
                    "url": url,
                    "final_url": result.final_url,
                    "sha256": result.sha256,
                    "bytes": result.bytes_received,
                    "headers": result.headers,
                }
            )
            return None

        primary = source.primary_filename
        if stopped := get(primary, source.url):
            return stopped
        first = paths[primary].read_bytes()
        if not force_download and reusable(previous, {primary: first}):
            shutil.copyfile(previous, destination)
            with destination.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            components.append({"name": "*", "reused_archive_sha256": digest})
            return FetchResponse(
                200,
                {"content-type": "application/zip"},
                source.url,
                received,
                digest,
                tuple(components),
            )
        try:
            plan = parser.ballot_items(first, source)
            urls = [(member, item_url(source.url, identity)) for member, identity in plan]
        except (ValueError, UnicodeError) as error:
            raise FetchFailure(str(error), retryable=False) from error
        if {member for member, _ in plan} != set(source.supplemental_members) or len(plan) != len(
            source.supplemental_members
        ):
            raise FetchFailure(
                "Ballot item plan differs from the configured members", retryable=False
            )
        for member, url in urls:
            if stopped := get(member, url):
                return stopped
        check = ".recheck.json"
        if stopped := get(check, source.url):
            return stopped
        if paths.pop(check).read_bytes() != first:
            raise FetchFailure("Official results changed during collection; try again")
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(paths.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    with destination.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return FetchResponse(
        200, {"content-type": "application/zip"}, source.url, received, digest, tuple(components)
    )
