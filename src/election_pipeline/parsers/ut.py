"""Utah Lieutenant Governor's Office results (Enhanced Voting): totals by county.

Utah's results site does not break counts down by ballot type, so this is a totals-only
feed. Utah has no U.S. Senate or governor race in 2026; all four U.S. House seats are
covered. Like Washington, Utah leaves the "vote for" field empty, so it is not checked;
each is a single-seat contest. The site's jurisdiction short name is "Utah" while its
API path uses "utah".
"""

import re

from election_pipeline.parsers import enhanced_voting as ev


def race(name):
    match = re.fullmatch(r"U\.S\. House District ([1-4])", name)
    return f"UT-0{match[1]}" if match else None


# Utah's site names each party but gives no abbreviation.
PARTIES = {
    "Democratic": "DEM",
    "Republican": "REP",
    "Libertarian": "LIB",
    "Constitution": "CON",
    "Forward": "FWD",
    "Independent American": "IAP",
    "Unaffiliated": "UNA",
}
SITE = ev.Site("UT", "Utah", "Utah", "General11032026", (), race, ev.strip_county, parties=PARTIES)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
