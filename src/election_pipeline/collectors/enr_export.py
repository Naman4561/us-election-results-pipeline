"""Official ASP.NET election-night sites (South Dakota, New Mexico).

County results for statewide races are offered only as workbook downloads behind each
page's own export buttons. The collector GETs the official page, then posts that page's
hidden form fields back to the same page with one export button's event target, exactly
as the button does. It sends back only the cookies those pages set during this
collection. Nothing is solved, guessed or retried around a challenge: any other response
is a fetch failure and the last publication stays live.

The state's parser module declares:

- ``CHANGE_MEMBERS``: statewide CSV members that change whenever any result changes;
- ``exports(paths, source)``: ``(member, page_member, event_target)`` for each workbook.

Each workbook embeds its download time, so identical results would otherwise create a
new snapshot every poll. When every change member is byte-identical to the previous
bundle and that bundle is under ``REFRESH_SECONDS`` old, the previous bundle is returned
unchanged. Otherwise every page and export is fetched again.
"""

import hashlib
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import replace
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from election_pipeline.collect import FetchFailure, FetchResponse, fetch_file
from election_pipeline.parsers.html import Tree
from election_pipeline.states import state_adapter

REFRESH_SECONDS = 3600
WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TARGET = re.compile(r"ctl00\$MainContent\$[A-Za-z0-9_$]{1,120}")


def postback(page, page_url, target):
    """Return (action URL, encoded form) for one export button on an official page."""
    if not TARGET.fullmatch(target):
        raise ValueError("Unexpected export control name")
    forms = Tree(page).root.find("form")
    if len(forms) != 1:
        raise ValueError("Results page must contain exactly one form")
    action = urljoin(page_url, forms[0].attrs.get("action") or page_url)
    if (urlsplit(action).scheme, urlsplit(action).netloc.lower()) != (
        urlsplit(page_url).scheme,
        urlsplit(page_url).netloc.lower(),
    ) or urlsplit(action).path.lower() != urlsplit(page_url).path.lower():
        raise ValueError("Export form posts somewhere other than its own page")
    fields = {}
    for node in forms[0].find("input"):
        if node.attrs.get("type", "").lower() != "hidden":
            continue
        name = node.attrs.get("name")
        if not name or name in fields:
            raise ValueError("Missing or duplicate hidden form field")
        fields[name] = node.attrs.get("value") or ""
    if not {"__VIEWSTATE", "__EVENTVALIDATION"} <= fields.keys():
        raise ValueError("Results page lacks its export form state")
    fields.update(__EVENTTARGET=target, __EVENTARGUMENT="")
    return action, urlencode(fields).encode("ascii")


def reusable(previous, changes):
    if previous is None or not Path(previous).exists():
        return False
    if time.time() - Path(previous).stat().st_mtime > REFRESH_SECONDS:
        return False
    try:
        with zipfile.ZipFile(previous) as archive:
            return all(archive.read(name) == body for name, body in changes.items())
    except (KeyError, OSError, zipfile.BadZipFile):
        return False


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    urls = dict((*source.files, (source.primary_filename, source.url)))
    if set(parser.CHANGE_MEMBERS) - urls.keys():
        raise FetchFailure("Change members must be configured source files", retryable=False)
    jar, components, received = CookieJar(), [], 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination.parent) as directory:
        paths = {}

        def get(name, url, form=None):
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
            result = fetch_file(source, limits, path, {}, url=url, form=form, cookies=jar)
            if result.status != 200:
                return result
            received += result.bytes_received
            if form is not None and not result.headers.get("content-type", "").startswith(WORKBOOK):
                raise FetchFailure(f"Official export for {name} did not return a workbook")
            paths[name] = path
            components.append(
                {
                    "name": name,
                    "url": url,
                    "method": "GET" if form is None else "POST",
                    "final_url": result.final_url,
                    "sha256": result.sha256,
                    "bytes": result.bytes_received,
                    "headers": result.headers,
                }
            )
            return None

        for name in parser.CHANGE_MEMBERS:
            if stopped := get(name, urls[name]):
                return stopped
        changes = {name: paths[name].read_bytes() for name in parser.CHANGE_MEMBERS}
        if not force_download and reusable(previous, changes):
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
        for name, url in urls.items():
            if name not in paths and (stopped := get(name, url)):
                return stopped
        try:
            plan = parser.exports(paths, source)
        except (ValueError, UnicodeError) as error:
            raise FetchFailure(str(error), retryable=False) from error
        if {member for member, _, _ in plan} != set(source.supplemental_members):
            raise FetchFailure("Export plan differs from the configured members", retryable=False)
        for member, page_member, target in plan:
            # Like a person clicking several export buttons, each post reuses the page's state.
            try:
                action, form = postback(
                    paths[page_member].read_text(encoding="utf-8", errors="replace"),
                    urls[page_member],
                    target,
                )
            except ValueError as error:
                raise FetchFailure(str(error), retryable=False) from error
            if stopped := get(member, action, form):
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
