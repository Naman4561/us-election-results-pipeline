"""Utah Lieutenant Governor's Office results (Enhanced Voting): totals by county.

Utah's results site does not break counts down by ballot type, so this is a totals-only
feed. Utah has no U.S. Senate or governor race in 2026; all four U.S. House seats are
covered. Like Washington, Utah leaves the "vote for" field empty, so it is not checked;
each is a single-seat contest. The site's jurisdiction short name is "Utah" while its
API path uses "utah".
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/UT.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
