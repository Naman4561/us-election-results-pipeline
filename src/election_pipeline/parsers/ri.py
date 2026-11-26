"""Rhode Island Board of Elections results (Enhanced Voting): statewide and district totals.

Rhode Island elects a senator, a governor and two U.S. House members in 2026. Its site
breaks counts down by city and town, plus a "Federal" locality for ballots cast only in
federal races, which belong to no town or county. Counties are not reported, so only
statewide and district totals are kept; every locality's counts are still read and must
add up exactly to them, in total and by vote method (Election Day, early voting, mail
ballots), which are stored. Write-in votes are one "Write-in" line, kept as published.
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/RI.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
