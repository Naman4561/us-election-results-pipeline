"""One bounded HTTP attempt. Retry and cache policy belong to pipeline.py."""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)

from election_pipeline.settings import CollectorSettings, Source, validate_url
from election_pipeline.tls import context_for_url


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


class CheckedRedirect(HTTPRedirectHandler):
    """Check every redirect target before it is requested, not only the final URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_url(newurl)
        except ValueError as error:
            raise FetchFailure(
                f"Refused redirect to a disallowed URL: {error}", retryable=False, status=code
            ) from error
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def connect_ipv6_first(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """socket.create_connection, trying the host's IPv6 addresses before its IPv4 ones.

    With only a private (ULA) IPv6 address, as in a job container, the system's address
    order puts IPv4 first for public hosts; a source that needs IPv6 asks for this."""
    host, port = address
    addresses = sorted(
        socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM),
        key=lambda a: a[0] != socket.AF_INET6,
    )
    errors = []
    for family, kind, protocol, _, target in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            return sock
        except OSError as error:
            sock.close()
            errors.append(error)
    raise errors[-1] if errors else OSError(f"No address for {host}")


class IPv6FirstConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = connect_ipv6_first


class IPv6FirstHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(IPv6FirstConnection, req, context=self._context)


def open_url(request, *, timeout, context=None, cookies=None, prefer_ipv6=False):
    """Open with verified TLS (the host's context when it needs one) and checked redirects."""
    https = IPv6FirstHTTPSHandler if prefer_ipv6 else HTTPSHandler
    handlers = [
        CheckedRedirect(),
        https(context=context) if context is not None else https(),
    ]
    if cookies is not None:
        handlers.append(HTTPCookieProcessor(cookies))
    return build_opener(*handlers).open(request, timeout=timeout)


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
    form: bytes | None = None,
    cookies=None,
) -> FetchResponse:
    """GET by default. ``form`` posts an official page's own form back to it; ``cookies``
    is a per-collection jar so that post reaches the session that rendered the page."""
    validate_url(url or source.url)
    request = Request(
        url or source.url,
        data=form,
        method="GET" if form is None else "POST",
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/zip, text/csv, application/octet-stream;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
            **({} if form is None else {"Content-Type": "application/x-www-form-urlencoded"}),
            **conditional_headers,
        },
    )
    started = time.monotonic()
    try:
        context = context_for_url(url or source.url)
    except (ValueError, OSError) as error:
        raise FetchFailure(f"TLS configuration failed: {error}", retryable=False) from error
    try:
        # HTTPSHandler() without a context still uses verified default TLS.
        options = {"context": context} if context is not None else {}
        if cookies is not None:
            options["cookies"] = cookies
        if source.prefer_ipv6:
            options["prefer_ipv6"] = True
        response = open_url(request, timeout=settings.timeout_seconds, **options)
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
