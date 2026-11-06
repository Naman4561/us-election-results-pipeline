"""Official Enhanced Voting results sites (Virginia, Idaho, Washington).

The public results API serves one ``data`` document with every contest's statewide
totals, and one ``ballot-items/<id>`` document per contest with its county (locality)
breakdown. The collector GETs ``data`` (the configured source URL), asks the state's
parser which contests it needs, then GETs exactly those ballot items from the same
election path. These are the same documents the official results page loads; nothing is
posted, solved or guessed.

The state's parser module declares ``ballot_items(raw_data, source)``, which returns
``(member, ballot_item_id)`` for each selected contest.

If ``data`` is byte-identical to the previous bundle's (and that bundle is under
``REFRESH_SECONDS`` old) the previous bundle is reused. After the ballot items are
fetched, ``data`` is fetched once more: if it changed meanwhile, the site published new
results during collection, so the attempt fails (retryably) instead of pairing statewide
totals and county detail from two different publications.
"""

import re
from urllib.parse import urlsplit

from election_pipeline.collect import FetchFailure
from election_pipeline.collectors.common import Bundle
from election_pipeline.states import state_adapter

ITEM = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATA_PATH = re.compile(
    r"/results/public/api/elections/[A-Za-z0-9_-]{1,40}/[A-Za-z0-9_-]{1,80}/data"
)


def item_url(data_url, identity):
    if not ITEM.fullmatch(identity):
        raise ValueError("Unexpected ballot item identifier")
    parts = urlsplit(data_url)
    if parts.scheme != "https" or parts.query or not DATA_PATH.fullmatch(parts.path):
        raise ValueError("Source URL is not an Enhanced Voting election data document")
    return f"https://{parts.netloc}{parts.path[: -len('data')]}ballot-items/{identity}"


def fetch_bundle(source, settings, destination, *, previous=None, store=None, force_download=False):
    parser = state_adapter(source.state).parser
    if source.files:
        raise FetchFailure("Enhanced Voting bundles take no reference files", retryable=False)
    json_only = ("application/json",)
    with Bundle(source, settings, destination) as bundle:
        primary = source.primary_filename
        if stopped := bundle.get(primary, source.url, kinds=json_only):
            return stopped
        first = bundle.read(primary)
        if reused := bundle.reuse(previous, {primary: first}, force=force_download):
            return reused
        try:
            plan = parser.ballot_items(first, source)
            urls = [(member, item_url(source.url, identity)) for member, identity in plan]
        except (ValueError, UnicodeError) as error:
            raise FetchFailure(str(error), retryable=False) from error
        members = [member for member, _ in plan]
        if sorted(members) != sorted(source.supplemental_members):
            raise FetchFailure(
                "Ballot item plan differs from the configured members", retryable=False
            )
        for member, url in urls:
            if stopped := bundle.get(member, url, kinds=json_only):
                return stopped
        check = ".recheck.json"
        if stopped := bundle.get(check, source.url, kinds=json_only):
            return stopped
        if bundle.read(check) != first:
            raise FetchFailure("Official results changed during collection; try again")
        return bundle.finish(exclude={check})
