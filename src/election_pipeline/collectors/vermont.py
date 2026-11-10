"""Vermont Secretary of State results (electionresults.vermont.gov).

The official results page loads the election's index (the configured source URL), which
names the current publication's files; their names carry its time stamp and change with
every publication. The collector GETs the index, then the federal and statewide offices
files it names, then the index once more: if the publication changed meanwhile, the
attempt fails (retryably) rather than pairing files from two publications. Nothing is
posted or guessed.
"""

import json
import re
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import Bundle

HOST = "static.electionresults.vermont.gov"
FILES = {"federal.json": "federal", "statewide.json": "stateWide"}
# The site serves its JSON as a generic binary type.
KINDS = ("application/octet-stream", "application/json")


def publication(raw, index_url):
    """{member: URL} of the files the index names for the current publication."""
    parts = urlsplit(index_url)
    match = re.fullmatch(r"/elections/([0-9a-f-]{36})\.json", parts.path)
    if parts.scheme != "https" or parts.netloc != HOST or parts.query or not match:
        raise ValueError("Source URL is not a Vermont election index")
    try:
        index = json.loads(raw.decode("utf-8-sig"))
        paths = {member: index[key]["path"] for member, key in FILES.items()}
        enabled = all(index[key]["isEnable"] is True for key in FILES.values())
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("Unexpected Vermont election index") from error
    urls = {}
    for member, path in paths.items():
        name = re.fullmatch(
            r"elections\\(" + re.escape(match[1]) + r"-(?:f|s)-[0-9]{14}\.json)", path or ""
        )
        if not enabled or not name:
            raise ValueError("Vermont index names an unexpected results file")
        urls[member] = f"https://{HOST}/elections/{name[1]}"
    return urls


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    if source.files or set(source.supplemental_members) != set(FILES):
        raise FetchFailure("Vermont bundles hold the index and its two files", retryable=False)
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url, kinds=KINDS):
            return stopped
        first = bundle.read(primary)
        if reused := bundle.reuse(previous, {primary: first}, force=force_download):
            return reused
        try:
            urls = publication(first, source.url)
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        for member, url in urls.items():
            if stopped := bundle.get(member, url, kinds=KINDS):
                return stopped
        check = ".recheck.json"
        if stopped := bundle.get(check, source.url, kinds=KINDS):
            return stopped
        try:
            same = publication(bundle.read(check), source.url) == urls
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        if not same:
            raise FetchFailure("Official results changed during collection; try again")
        return bundle.finish(exclude={check})
