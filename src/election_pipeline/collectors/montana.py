"""Montana Secretary of State: the results site's Statewide Export workbook.

Montana's site is the ASP.NET platform of South Dakota and New Mexico, without their
statewide CSV, so nothing small and stable says whether results changed. The collector
GETs the export page and posts its "Statewide Export" button back (as ``enr_export``
does: the page's own hidden fields, its own cookies, nothing solved or retried around a
challenge). Every download differs in its "Downloaded at" time, its relationship IDs
and a package file name, while the worksheets stay byte-identical; ``results_key``
compares only the worksheets and the shared text without that time. When it matches the
previous bundle's and that bundle is under ``REFRESH_SECONDS`` old, the previous bundle
is returned unchanged.
"""

import hashlib
import io
import re
import shutil
import time
import zipfile
from http.cookiejar import CookieJar
from pathlib import Path

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import REFRESH_SECONDS, Bundle, digest
from election_pipeline.collectors.enr_export import WORKBOOK, postback
from election_pipeline.states import state_adapter

DOWNLOADED = re.compile(rb"<x:t>Downloaded at [0-9/]+ [0-9:]+ [AP]M</x:t>")


def results_key(raw):
    """A fingerprint of a Statewide Export's results: its worksheets and shared text,
    without the download time. Raises ValueError for anything but such a workbook."""
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        names = sorted(
            n for n in book.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        shared = book.read("xl/sharedStrings.xml")
        if not names or len(DOWNLOADED.findall(shared)) != 1:
            raise ValueError("Montana export is not the Statewide Export workbook")
        key = hashlib.sha256(DOWNLOADED.sub(b"", shared))
        for name in names:
            key.update(b"\0" + name.encode() + b"\0" + book.read(name))
    return key.hexdigest()


def unchanged(previous, key, member):
    """True when the previous bundle is recent and its workbook has the same results."""
    if previous is None or not Path(previous).exists():
        return False
    if time.time() - Path(previous).stat().st_mtime > REFRESH_SECONDS:
        return False
    try:
        with zipfile.ZipFile(previous) as archive:
            return results_key(archive.read(member)) == key
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return False


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    with Bundle(source, settings, destination, cookies=CookieJar(), record_method=True) as bundle:
        if stopped := bundle.get(source.primary_filename, source.url):
            return stopped
        page = bundle.read(source.primary_filename).decode("utf-8", errors="replace")
        try:
            action, form = postback(page, source.url, parser.export_target(page))
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        if stopped := bundle.get(parser.EXPORT, action, form=form, kinds=(WORKBOOK,)):
            return stopped
        try:
            key = results_key(bundle.read(parser.EXPORT))
        except (ValueError, KeyError, zipfile.BadZipFile) as error:
            raise FetchFailure(str(error), retryable=False) from error
        if not force_download and unchanged(previous, key, parser.EXPORT):
            shutil.copyfile(previous, bundle.destination)
            sha256 = digest(bundle.destination)
            bundle.components.append({"name": "*", "reused_archive_sha256": sha256})
            return bundle.response(sha256)
        return bundle.finish()
