"""One bounded HTTP attempt. Retry and cache policy belong to pipeline.py."""

from __future__ import annotations

import hashlib
import http.client
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from election_pipeline.settings import CollectorSettings, Source, validate_url


class FetchFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status: int | None = None,
        headers: dict | None = None,
        bytes_received: int = 0,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.headers = headers or {}
        self.bytes_received = bytes_received


@dataclass(frozen=True)
class FetchResponse:
    status: int
    headers: dict[str, str]
    final_url: str
    bytes_received: int = 0
    sha256: str | None = None
    components: tuple[dict, ...] = ()


def fetch_file(
    source: Source,
    settings: CollectorSettings,
    temporary: Path,
    conditional_headers: dict[str, str],
    *,
    url: str | None = None,
) -> FetchResponse:
    validate_url(url or source.url)
    request = Request(
        url or source.url,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/zip, text/csv, application/octet-stream;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
            **conditional_headers,
        },
    )
    started = time.monotonic()
    try:
        response = urlopen(request, timeout=settings.timeout_seconds)
    except HTTPError as error:
        # urllib represents 304 as HTTPError; it is a normal conditional-GET result.
        response = error
    except (URLError, OSError, http.client.HTTPException) as error:
        raise FetchFailure(f"Request failed: {error}") from error

    with response:
        status = response.status
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "etag",
                "last-modified",
                "content-type",
                "content-length",
                "retry-after",
                "date",
                "age",
                "cache-control",
            }
        }
        final_url = response.geturl()
        validate_url(final_url)
        if status != 200:
            return FetchResponse(status, headers, final_url)

        length = headers.get("content-length")
        try:
            expected_length = int(length) if length is not None else None
        except ValueError as error:
            raise FetchFailure("Invalid Content-Length", status=status, headers=headers) from error
        if expected_length is not None and (
            expected_length < 0 or expected_length > settings.max_download_bytes
        ):
            raise FetchFailure(
                "Response exceeds the configured size limit",
                retryable=False,
                status=status,
                headers=headers,
            )

        digest = hashlib.sha256()
        received = 0
        with temporary.open("wb") as handle:
            while True:
                if time.monotonic() - started > settings.max_download_seconds:
                    raise FetchFailure(
                        "Download exceeded its elapsed-time limit",
                        status=status,
                        headers=headers,
                        bytes_received=received,
                    )
                try:
                    # read1 returns available data rather than waiting for a full large chunk.
                    # A blocking socket read can still add up to timeout_seconds to the limit.
                    chunk = response.read1(128 * 1024)
                except (OSError, http.client.HTTPException) as error:
                    raise FetchFailure(
                        f"Response body interrupted: {error}",
                        status=status,
                        headers=headers,
                        bytes_received=received,
                    ) from error
                if not chunk:
                    break
                received += len(chunk)
                if received > settings.max_download_bytes:
                    raise FetchFailure(
                        "Response exceeded the configured size limit",
                        retryable=False,
                        status=status,
                        headers=headers,
                        bytes_received=received,
                    )
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_length is not None and received != expected_length:
            raise FetchFailure(
                "Response body length does not match Content-Length",
                status=status,
                headers=headers,
                bytes_received=received,
            )
        return FetchResponse(status, headers, final_url, received, digest.hexdigest())
