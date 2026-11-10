"""Rhode Island Board of Elections results (Enhanced Voting): statewide and district totals.

Rhode Island elects a senator, a governor and two U.S. House members in 2026. Its site
breaks counts down by city and town, plus a "Federal" locality for ballots cast only in
federal races, which belong to no town or county. Counties are not reported, so only
statewide and district totals are kept; every locality's counts are still read and must
add up exactly to them, in total and by vote method (Election Day, early voting, mail
ballots), which are stored. Write-in votes are one "Write-in" line, kept as published.
"""

import re

from election_pipeline.parsers import enhanced_voting as ev


def race(name):
    if name == "Senator in Congress":
        return "RI-SEN"
    if name == "Governor":
        return "RI-GOV"
    match = re.fullmatch(r"Representative in Congress District ([12])", name)
    return f"RI-0{match[1]}" if match else None


METHODS = {
    "Election Day": "election_day",
    "Early Voting": "early_voting",
    "Mail Ballots": "absentee_by_mail",
}
SITE = ev.Site(
    "RI",
    "Rhode Island",
    "RhodeIsland",
    "2026StatewideGeneralElection",
    ("Vote for 1",),
    race,
    None,
    methods=METHODS,
)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
