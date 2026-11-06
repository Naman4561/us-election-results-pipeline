"""Official ASP.NET election-night sites (South Dakota, New Mexico).

County results for statewide races are offered only as workbook downloads behind each
page's own export buttons. The collector GETs the official page, then posts that page's
hidden form fields back to the same page with one export button's event target, exactly
as the button does. It sends back only the cookies those pages set during this
collection. Nothing is solved, guessed or retried around a challenge: any other response
is a fetch failure and the last publication stays live.

The state's parser module declares:

- ``CHANGE_MEMBERS``: statewide CSV members that change whenever any result changes;
- ``exports(paths, source)``: ``(member, page_member, event_target)`` for each export,
  optionally with a fourth item: the page's own radio choices to submit with it (for
  example Oregon's "CSV" format option). A choice must be a radio button that exists on
  that page with exactly that value.

An export named ``*.xlsx`` must come back as a workbook and ``*.csv`` as a CSV download.

Each workbook embeds its download time, so identical results would otherwise create a
new snapshot every poll. When every change member is byte-identical to the previous
bundle and that bundle is under ``REFRESH_SECONDS`` old, the previous bundle is returned
unchanged. Otherwise every page and export is fetched again.
"""

import re
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import REFRESH_SECONDS, Bundle, reusable  # noqa: F401
from election_pipeline.parsers.html import Tree
from election_pipeline.states import state_adapter

WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TARGET = re.compile(r"ctl00\$MainContent\$[A-Za-z0-9_$]{1,120}")
DOWNLOADS = {".xlsx": (WORKBOOK,), ".csv": ("application/text", "text/csv", "application/csv")}


def postback(page, page_url, target, choices=None):
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
    for name, value in (choices or {}).items():
        radios = [
            n
            for n in forms[0].find("input")
            if n.attrs.get("type", "").lower() == "radio"
            and n.attrs.get("name") == name
            and n.attrs.get("value") == value
        ]
        if len(radios) != 1 or name in fields:
            raise ValueError("Export option is not one of the page's own choices")
        fields[name] = value
    fields.update(__EVENTTARGET=target, __EVENTARGUMENT="")
    return action, urlencode(fields).encode("ascii")


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    urls = dict((*source.files, (source.primary_filename, source.url)))
    if set(parser.CHANGE_MEMBERS) - urls.keys():
        raise FetchFailure("Change members must be configured source files", retryable=False)
    with Bundle(source, settings, destination, cookies=CookieJar(), record_method=True) as bundle:
        for name in parser.CHANGE_MEMBERS:
            if stopped := bundle.get(name, urls[name]):
                return stopped
        changes = {name: bundle.read(name) for name in parser.CHANGE_MEMBERS}
        if reused := bundle.reuse(previous, changes, force=force_download):
            return reused
        for name, url in urls.items():
            if name not in bundle.paths and (stopped := bundle.get(name, url)):
                return stopped
        try:
            plan = parser.exports(bundle.paths, source)
        except (ValueError, UnicodeError) as error:
            raise FetchFailure(str(error), retryable=False) from error
        if {entry[0] for entry in plan} != set(source.supplemental_members):
            raise FetchFailure("Export plan differs from the configured members", retryable=False)
        for member, page_member, target, *choices in plan:
            # Like a person clicking several export buttons, each post reuses the page's state.
            try:
                action, form = postback(
                    bundle.paths[page_member].read_text(encoding="utf-8", errors="replace"),
                    urls[page_member],
                    target,
                    *choices,
                )
            except ValueError as error:
                raise FetchFailure(str(error), retryable=False) from error
            kinds = DOWNLOADS.get(Path(member).suffix, (WORKBOOK,))
            if stopped := bundle.get(member, action, form=form, kinds=kinds):
                return stopped
        return bundle.finish()
