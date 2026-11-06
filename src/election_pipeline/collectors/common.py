"""Shared mechanics for multi-file bundle collectors.

Every bundle collector downloads several official files within one source's elapsed-time
and byte budgets, records each component's provenance, and writes a deterministic zip
(fixed member timestamps, sorted names) so identical files always hash identically.
``Bundle`` does that bookkeeping once; each collector only decides what to fetch.
"""

import hashlib
import shutil
import tempfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path

from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file

# A reused bundle is refreshed at least this often, so a change outside the change
# members (for example a county-only correction) still appears within this interval.
REFRESH_SECONDS = 3600


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def reusable(previous, changes, refresh_seconds=REFRESH_SECONDS):
    """True when the previous bundle is recent and holds byte-identical change members."""
    if previous is None or not Path(previous).exists():
        return False
    if time.time() - Path(previous).stat().st_mtime > refresh_seconds:
        return False
    try:
        with zipfile.ZipFile(previous) as archive:
            return all(archive.read(name) == body for name, body in changes.items())
    except (KeyError, OSError, zipfile.BadZipFile):
        return False


class Bundle:
    """One bundle collection: budgeted downloads into a private temporary directory."""

    def __init__(self, source, settings, destination, *, cookies=None, record_method=False):
        self.source, self.settings, self.destination = source, settings, Path(destination)
        self.cookies, self.record_method = cookies, record_method
        self.components, self.paths, self.urls = [], {}, {}
        self.received = 0
        self.started = time.monotonic()
        self._directory = tempfile.TemporaryDirectory(
            prefix=".bundle-", dir=self.destination.parent
        )
        self.directory = Path(self._directory.name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._directory.cleanup()
        return False

    def get(self, name, url, *, form=None, kinds=None):
        """Download one member. Returns the response to stop on (non-200), else None."""
        remaining = self.settings.max_download_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise FetchFailure("Bundle exceeded its elapsed-time budget")
        if self.received >= self.settings.max_download_bytes:
            raise FetchFailure("Bundle exceeds its combined download budget", retryable=False)
        limits = replace(
            self.settings,
            max_download_seconds=remaining,
            timeout_seconds=min(self.settings.timeout_seconds, remaining),
            max_download_bytes=self.settings.max_download_bytes - self.received,
        )
        path = self.directory / name
        options = {"url": url}
        if form is not None:
            options["form"] = form
        if self.cookies is not None:
            options["cookies"] = self.cookies
        result = fetch_file(self.source, limits, path, {}, **options)
        if result.status != 200:
            return result
        self.received += result.bytes_received
        if self.received > self.settings.max_download_bytes:
            raise FetchFailure("Bundle exceeds its combined download budget", retryable=False)
        if kinds and not result.headers.get("content-type", "").startswith(kinds):
            raise FetchFailure(f"Official source returned an unexpected file type for {name}")
        self.paths[name], self.urls[name] = path, url
        component = {"name": name, "url": url}
        if self.record_method:
            component["method"] = "GET" if form is None else "POST"
        component.update(
            final_url=result.final_url,
            sha256=result.sha256,
            bytes=result.bytes_received,
            headers=result.headers,
        )
        self.components.append(component)
        return None

    def read(self, name):
        return self.paths[name].read_bytes()

    def response(self, sha256):
        return FetchResponse(
            200,
            {"content-type": "application/zip"},
            self.source.url,
            self.received,
            sha256,
            tuple(self.components),
        )

    def reuse(self, previous, changes, *, force=False):
        """Copy the previous bundle when its change members are unchanged; else None."""
        if force or not reusable(previous, changes):
            return None
        shutil.copyfile(previous, self.destination)
        sha256 = digest(self.destination)
        self.components.append({"name": "*", "reused_archive_sha256": sha256})
        return self.response(sha256)

    def finish(self, exclude=()):
        """Write the deterministic zip of every downloaded member except ``exclude``."""
        with zipfile.ZipFile(self.destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(self.paths.items()):
                if name in exclude:
                    continue
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
        return self.response(digest(self.destination))
