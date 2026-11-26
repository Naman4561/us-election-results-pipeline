"""Georgia Secretary of State results (Enhanced Voting): totals by county.

Georgia reports its 159 counties, whose names drop their " County" suffix to match the
Census county map. It splits most counts into ballot groups (Election Day, Advance
Voting, Absentee by Mail, Provisional), but leaves the split blank in some counties
(Ben Hill, Miller, Lumpkin and Glascock in its official results), so only totals are
kept.
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/GA.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
