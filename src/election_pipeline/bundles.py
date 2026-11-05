"""Pin a multi-file source in one deterministic, compressed, replayable archive."""

import hashlib
import tempfile
import time
import zipfile
from dataclasses import replace
from importlib import import_module
from pathlib import Path

from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file
from election_pipeline.states import STATES


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    adapter = STATES.get(source.state)
    if adapter and adapter.collection_backend:
        backend = import_module("election_pipeline.collectors." + adapter.collection_backend)
        return backend.fetch_bundle(
            source,
            settings,
            destination,
            previous=previous,
            store=store,
            force_download=force_download,
        )
    components, received = [], 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as directory:
        paths = {}
        for name, url in (*source.files, (source.primary_filename, source.url)):
            adapter = STATES.get(source.state)
            if name == source.primary_filename and adapter and adapter.bundle_resolver:
                resolver = import_module("election_pipeline.collectors." + adapter.bundle_resolver)
                try:
                    url = resolver.resolve(source, paths)
                except (ValueError, UnicodeError) as error:
                    raise FetchFailure(str(error), retryable=False) from error
            path = Path(directory) / name
            remaining = settings.max_download_seconds - (time.monotonic() - started)
            if received >= settings.max_download_bytes:
                raise FetchFailure("Bundle exceeds its combined download budget", retryable=False)
            if remaining <= 0:
                raise FetchFailure("Bundle exceeded its elapsed-time budget")
            limits = replace(
                settings,
                max_download_seconds=remaining,
                timeout_seconds=min(settings.timeout_seconds, remaining),
                max_download_bytes=settings.max_download_bytes - received,
            )
            result = fetch_file(source, limits, path, {}, url=url)
            if result.status != 200:
                return result
            received += result.bytes_received
            if received > settings.max_download_bytes:
                raise FetchFailure("Bundle exceeds its combined download budget", retryable=False)
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
