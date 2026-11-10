"""Arkansas Secretary of State results (Tally): totals by county.

Arkansas elects a senator, a governor and four U.S. House members in 2026. Its federal
contests are published as contest type "Federal" and the governor as "Statewide".
Candidate names carry a party prefix ("REP Tom Cotton"), dropped where it is the
candidate's own party.
"""

import re

from election_pipeline.parsers import tally

TYPES = {
    "AR-SEN": "Federal",
    "AR-GOV": "Statewide",
    **{f"AR-0{n}": "Federal" for n in range(1, 5)},
}


def race(name):
    if name == "U.S. Senate":
        return "AR-SEN"
    if name == "Governor":
        return "AR-GOV"
    match = re.fullmatch(r"U\.S\. Congress District 0([1-4])", name)
    return f"AR-0{match[1]}" if match else None


SITE = tally.Site(
    "AR",
    "Arkansas",
    "05",
    "11b07106-031e-4d7c-911a-d01fb473d6dd",
    "2026 General Election & Judical Runoff",
    race,
    TYPES,
)


def parse(archive_path, source, settings, output, stats, reference=None):
    tally.parse_site(SITE, archive_path, source, settings, output, stats, reference)
