"""Official Clarity (Scytl) election-night sites (Colorado, Iowa, West Virginia, and South
Carolina, whose site is served from its own host).

A Clarity election publishes numbered versions. ``current_ver.txt`` (the configured source
URL) names the current one; that version's files live under ``<election>/<version>/``. The
collector reads the version, then GETs the parser's ``VERSION_FILES`` from that version
only, so every file in a bundle comes from one publication. These are the files the
official page itself downloads and links ("Download reports"). Nothing is posted or
guessed.

If ``current_ver.txt`` is byte-identical to the previous bundle's and that bundle is under
``REFRESH_SECONDS`` old, the previous bundle is reused.
"""

import re
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import Bundle
from election_pipeline.states import state_adapter

VERSION_URL = re.compile(r"/[A-Z]{2}(?:/[A-Za-z_]{1,40})?/[0-9]{1,9}/current_ver\.txt")
HOSTS = {"results.enr.clarityelections.com", "www.enr-scvotes.org"}


def version_base(url, raw):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc not in HOSTS
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
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url, kinds=("text/plain",)):
            return stopped
        current = bundle.read(primary)
        if reused := bundle.reuse(previous, {primary: current}, force=force_download):
            return reused
        try:
            base = version_base(source.url, current)
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        for member, relative in parser.VERSION_FILES.items():
            kinds = ("application/zip",) if member.endswith(".zip") else ("application/json",)
            if stopped := bundle.get(member, base + relative, kinds=kinds):
                return stopped
        return bundle.finish()
