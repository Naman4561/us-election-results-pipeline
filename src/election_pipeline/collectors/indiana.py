"""Indiana Election Division results (enr.indianavoters.in.gov).

The official results page loads ``data/settings.json`` (the configured source URL), which
names the current publication (``VersionCode``) and which of two alternating file sets
holds it (``VersionType``, a letter). The U.S. House results are that set's
``OffCatC_1005_<letter>.json``, the file the page loads for its "US Representative"
office. The collector GETs settings, then that file, then settings once more: if the
publication changed meanwhile, the attempt fails (retryably) rather than pairing a file
with the wrong publication. Nothing is posted or guessed.
"""

import json
import re
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import Bundle

SETTINGS_PATH = "/site/data/settings.json"
HOST = "enr.indianavoters.in.gov"
HOUSE = "house.json"


def publication(raw):
    """(VersionCode, VersionType) of an official settings document."""
    try:
        root = json.loads(raw.decode("utf-8-sig"))["Root"]
        code, letter = root["VersionCode"], root["VersionType"]
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("Unexpected Indiana settings document") from error
    if not (
        isinstance(code, str)
        and re.fullmatch(r"2026[01][0-9][0-3][0-9]_[0-9]{6}", code)
        and isinstance(letter, str)
        and re.fullmatch(r"[A-Z]", letter)
    ):
        raise ValueError("Unexpected Indiana publication version")
    return code, letter


def house_url(settings_url, letter):
    parts = urlsplit(settings_url)
    if (
        parts.scheme != "https"
        or parts.netloc != HOST
        or parts.query
        or parts.path != SETTINGS_PATH
    ):
        raise ValueError("Source URL is not Indiana's results settings document")
    return f"https://{HOST}/site/data/OffCatC_1005_{letter}.json"


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    if source.files or set(source.supplemental_members) != {HOUSE}:
        raise FetchFailure("Indiana bundles hold settings and the House file", retryable=False)
    json_only = ("application/json",)
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url, kinds=json_only):
            return stopped
        first = bundle.read(primary)
        if reused := bundle.reuse(previous, {primary: first}, force=force_download):
            return reused
        try:
            version = publication(first)
            url = house_url(source.url, version[1])
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        if stopped := bundle.get(HOUSE, url, kinds=json_only):
            return stopped
        check = ".recheck.json"
        if stopped := bundle.get(check, source.url, kinds=json_only):
            return stopped
        try:
            same = publication(bundle.read(check)) == version
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        if not same:
            raise FetchFailure("Official results changed during collection; try again")
        return bundle.finish(exclude={check})
