"""Idaho Secretary of State results (Enhanced Voting): totals by county.

Idaho's results site does not break counts down by ballot type, so this is a totals-only
feed. County names drop their " County" suffix to match the Census county map.
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/ID.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
