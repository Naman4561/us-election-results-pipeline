"""Virginia Department of Elections results (Enhanced Voting), with vote methods.

Virginia reports by locality: its 95 counties and 38 independent cities, which the
Census also treats as county equivalents. Locality names keep their "County"/"City"
suffix because several names exist as both (Fairfax, Franklin, Richmond, Roanoke).

Every candidate count is split into five ballot groups that add up to it: Early Voting,
Election Day, Mailed Absentee, Provisional and Post-Election (absentee ballots received
and counted after Election Day). All five are stored. Virginia has no governor's race in
2026.
"""

import re

from election_pipeline.parsers import enhanced_voting as ev

METHODS = {
    "Early Voting": "early_voting",
    "Election Day": "election_day",
    "Mailed Absentee": "absentee_by_mail",
    "Provisional": "provisional",
    "Post-Election": "post_election",
}


def ordinal(number):
    return "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def race(name):
    if name == "Member, United States Senate":
        return "VA-SEN"
    match = re.fullmatch(
        r"Member, House of Representatives \(([1-9][0-9]?)(st|nd|rd|th) District\)", name
    )
    if match and match[2] == ordinal(int(match[1])):
        return f"VA-{int(match[1]):02}"
    return None


def county(name):
    if not re.fullmatch(r"[A-Z][A-Za-z .'-]+ (COUNTY|CITY)", name):
        raise ValueError(f"Unexpected Virginia locality {name!r}")
    return name


SITE = ev.Site(
    "VA",
    "Virginia",
    "virginia",
    "2026-November-General",
    ("Vote for only one",),
    race,
    county,
    METHODS,
)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
