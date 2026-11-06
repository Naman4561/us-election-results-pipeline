"""Washington Secretary of State results (Enhanced Voting): totals by county.

Washington votes by mail and its results site does not split counts by ballot type, so
this is a totals-only feed. Washington has no U.S. Senate or governor race in 2026; all
ten U.S. House seats are covered. Washington's site fills the "vote for" field with a
category label ("Congressional") rather than a seat count, so it is not checked; each is
a single-seat, top-two general election contest.
Write-in votes are reported as one "Write-in" line and kept as a candidate, as published.
"""

import re

from election_pipeline.parsers import enhanced_voting as ev
from election_pipeline.parsers.totals import database_row, load_contract  # noqa: F401


def race(name):
    match = re.fullmatch(r"Congressional District ([1-9]|10) - U\.S\. Representative", name)
    return f"WA-{int(match[1]):02}" if match else None


SITE = ev.Site("WA", "Washington", "washington", "20261103", (), race, ev.strip_county)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
