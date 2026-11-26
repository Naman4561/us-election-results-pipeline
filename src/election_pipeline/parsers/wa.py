"""Washington Secretary of State results (Enhanced Voting): totals by county.

Washington votes by mail and its results site does not split counts by ballot type, so
this is a totals-only feed. Washington has no U.S. Senate or governor race in 2026; all
ten U.S. House seats are covered. Washington's site fills the "vote for" field with a
category label ("Congressional") rather than a seat count, so it is not checked; each is
a single-seat, top-two general election contest.
Write-in votes are reported as one "Write-in" line and kept as a candidate, as published.
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/WA.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
