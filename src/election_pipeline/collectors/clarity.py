"""Official Clarity (Scytl) election-night sites (Colorado, Iowa).

A Clarity election publishes numbered versions. ``current_ver.txt`` (the configured source
URL) names the current one; that version's files live under ``<election>/<version>/``. The
collector reads the version, then GETs the parser's ``VERSION_FILES`` from that version
only, so every file in a bundle comes from one publication. These are the files the
official page itself downloads and links ("Download reports"). Nothing is posted or
guessed.

If ``current_ver.txt`` is byte-identical to the previous bundle's and that bundle is under
``REFRESH_SECONDS`` old, the previous bundle is reused.
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

VERSION_URL = re.compile(r"/[A-Z]{2}(?:/[A-Za-z_]{1,40})?/[0-9]{1,9}/current_ver\.txt")


def version_base(url, raw):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "results.enr.clarityelections.com"
        or parts.query
        or not VERSION_URL.fullmatch(parts.path)
    ):
        raise ValueError("Source URL is not a Clarity current_ver.txt document")
    version = raw.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9]{1,9}", version):
        raise ValueError("Clarity returned an invalid version number")
    return f"https://{parts.netloc}{parts.path[: -len('current_ver.txt')]}{version}/"


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    if source.files:
        raise FetchFailure("Clarity bundles take no reference files", retryable=False)
    if set(parser.VERSION_FILES) != set(source.supplemental_members):
        raise FetchFailure("Clarity files differ from the configured members", retryable=False)
    components, received = [], 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as directory:
        paths = {}

        def get(name, url, kinds):
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
            if not result.headers.get("content-type", "").startswith(kinds):
                raise FetchFailure(f"Clarity returned an unexpected content type for {name}")
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
        if stopped := get(primary, source.url, ("text/plain",)):
            return stopped
        current = paths[primary].read_bytes()
        if not force_download and reusable(previous, {primary: current}):
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
            base = version_base(source.url, current)
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        for member, relative in parser.VERSION_FILES.items():
            kinds = ("application/zip",) if member.endswith(".zip") else ("application/json",)
            if stopped := get(member, base + relative, kinds):
                return stopped
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
