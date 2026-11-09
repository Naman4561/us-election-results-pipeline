"""Idaho Secretary of State results (Enhanced Voting): totals by county.

Idaho's results site does not break counts down by ballot type, so this is a totals-only
feed. County names drop their " County" suffix to match the Census county map.
"""

import re

from election_pipeline.parsers import enhanced_voting as ev


def race(name):
    if name == "United States Senator":
        return "ID-SEN"
    if name == "Governor":
        return "ID-GOV"
    match = re.fullmatch(r"United States Representative District ([12])", name)
    return f"ID-0{match[1]}" if match else None


SITE = ev.Site("ID", "Idaho", "id", "nov2026", ("Vote for 1",), race, ev.strip_county)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
