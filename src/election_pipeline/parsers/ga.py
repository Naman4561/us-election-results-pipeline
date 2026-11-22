"""Georgia Secretary of State results (Enhanced Voting): totals by county.

Georgia reports its 159 counties, whose names drop their " County" suffix to match the
Census county map. It splits most counts into ballot groups (Election Day, Advance
Voting, Absentee by Mail, Provisional), but leaves the split blank in some counties
(Ben Hill, Miller, Lumpkin and Glascock in its official results), so only totals are
kept.
"""

import re

from election_pipeline.parsers import enhanced_voting as ev


def race(name):
    if name in {"US Senate", "Governor"}:
        return {"US Senate": "GA-SEN", "Governor": "GA-GOV"}[name]
    match = re.fullmatch(r"US House of Representatives - District (\d+)", name)
    return f"GA-{int(match[1]):02}" if match else None


SITE = ev.Site(
    "GA",
    "Georgia",
    "Georgia",
    "11032026GeneralAndSpecialElection",
    ("Vote for 1",),
    race,
    ev.strip_county,
    skip_methods=True,
)


def ballot_items(raw, source):
    return ev.ballot_items(raw, source, SITE)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(SITE, archive_path, source, settings, output, stats, reference)
