"""Connecticut Secretary of the State results (ctemspublic.tgstg.net, "Public Reporting").

The official results app loads static files: the election's ``Version.json`` (the
configured source URL, ``{"Version": N}``), then that version's folder of data files.
The collector GETs the version, reuses the previous bundle while the version is
unchanged (refreshed hourly), else GETs the version's files and the version once more:
if it changed meanwhile, the attempt fails (retryably) rather than pairing files from
two versions. Nothing is posted or guessed.
"""

import json
import re
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import Bundle

HOST = "ctemspublic.tgstg.net"
FILES = {
    "lookup.json": "Lookupdata.json",
    "election.json": "election_Electiondata.json",
    "state.json": "stateVotes_Electiondata.json",
    "towns.json": "townVotes_Electiondata.json",
    "status.json": "townStatus_Electiondata.json",
}
KINDS = ("application/json",)


def version(raw, source_url):
    """The version folder's URL that ``Version.json`` names."""
    parts = urlsplit(source_url)
    match = re.fullmatch(r"(/ng-app/data/election/[0-9]{1,6})/Version\.json", parts.path)
    if parts.scheme != "https" or parts.netloc != HOST or parts.query or not match:
        raise ValueError("Source URL is not a Connecticut election version")
    try:
        number = json.loads(raw.decode("utf-8-sig"))["Version"]
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("Unexpected Connecticut version file") from error
    if type(number) is not int or number <= 0:
        raise ValueError("Unexpected Connecticut version number")
    return f"https://{HOST}{match[1]}/{number}/"


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    if source.files or set(source.supplemental_members) != set(FILES):
        raise FetchFailure("Connecticut bundles hold the version and its files", retryable=False)
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url, kinds=KINDS):
            return stopped
        first = bundle.read(primary)
        if reused := bundle.reuse(previous, {primary: first}, force=force_download):
            return reused
        try:
            folder = version(first, source.url)
        except ValueError as error:
            raise FetchFailure(str(error), retryable=False) from error
        for member, name in FILES.items():
            if stopped := bundle.get(member, folder + name, kinds=KINDS):
                return stopped
        check = ".recheck.json"
        if stopped := bundle.get(check, source.url, kinds=KINDS):
            return stopped
        if bundle.read(check) != first:
            raise FetchFailure("Official results changed during collection; try again")
        return bundle.finish(exclude={check})
