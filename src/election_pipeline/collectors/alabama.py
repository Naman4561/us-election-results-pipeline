"""Alabama Secretary of State election night results (www2.alabamavotes.gov).

The statewide results page (the configured source URL) has an "Export Data" link that
posts the page's results form back (as ``enr_export`` posts an export button: the
form's own hidden fields and the cookies this collection was given; nothing solved or
retried around a challenge). The page also holds the site's search forms, so only the
results form, ``form1``, is posted. The export, ``sosEnrExport.xlsx``, has every
contest's county results and each county's reporting statistics.

Each download is a newly written workbook. ``results_key`` compares its sheets and
shared text; while they match the previous bundle's and that bundle is under
``REFRESH_SECONDS`` old, the previous bundle is returned unchanged.
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

EXPORT = "export.xlsx"
TARGET = "hlnkExportData"
FORM = re.compile(r'<form name="form1"[^>]*>.*?</form>', re.S)


def results_form(page):
    """The page's results form alone, without the site's search forms."""
    forms = FORM.findall(page)
    if len(forms) != 1 or f"__doPostBack(&#39;{TARGET}&#39;,&#39;&#39;)" not in forms[0]:
        raise ValueError("Alabama results page lacks its one results form and export link")
    return forms[0]


def results_key(raw):
    """A fingerprint of an export's results: its worksheets and shared text."""
    with zipfile.ZipFile(io.BytesIO(raw)) as book:
        names = sorted(
            n for n in book.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        if not names:
            raise ValueError("Alabama export is not a results workbook")
        key = hashlib.sha256(book.read("xl/sharedStrings.xml"))
        for name in names:
            key.update(b"\0" + name.encode() + b"\0" + book.read(name))
    return key.hexdigest()


def unchanged(previous, key):
    if previous is None or not Path(previous).exists():
        return False
    if time.time() - Path(previous).stat().st_mtime > REFRESH_SECONDS:
        return False
    try:
        with zipfile.ZipFile(previous) as archive:
            return results_key(archive.read(EXPORT)) == key
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return False


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    if source.files or set(source.supplemental_members) != {EXPORT}:
        raise FetchFailure("Alabama bundles hold the results page and its export", retryable=False)
    with Bundle(source, settings, destination, cookies=CookieJar(), record_method=True) as bundle:
        if stopped := bundle.get(source.primary_filename, source.url):
            return stopped
        page = bundle.read(source.primary_filename).decode("utf-8", errors="replace")
        try:
            action, form = postback(
                results_form(page), source.url, TARGET, targets=re.compile(re.escape(TARGET))
            )
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        if stopped := bundle.get(EXPORT, action, form=form, kinds=(WORKBOOK,)):
            return stopped
        try:
            key = results_key(bundle.read(EXPORT))
        except (ValueError, KeyError, zipfile.BadZipFile) as error:
            raise FetchFailure(str(error), retryable=False) from error
        if not force_download and unchanged(previous, key):
            shutil.copyfile(previous, bundle.destination)
            sha256 = digest(bundle.destination)
            bundle.components.append({"name": "*", "reused_archive_sha256": sha256})
            return bundle.response(sha256)
        return bundle.finish()
