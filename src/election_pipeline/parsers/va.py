"""Virginia Department of Elections results (Enhanced Voting), with vote methods.

Virginia reports by locality: its 95 counties and 38 independent cities, which the
Census also treats as county equivalents. Locality names keep their "County"/"City"
suffix because several names exist as both (Fairfax, Franklin, Richmond, Roanoke).

Every candidate count is split into five ballot groups that add up to it: Early Voting,
Election Day, Mailed Absentee, Provisional and Post-Election (absentee ballots received
and counted after Election Day). All five are stored. Virginia has no governor's race in
2026.
The site's identity, contest titles and county names are in the election definition
(``[platform]`` of ``states/VA.toml``).
"""

from election_pipeline.parsers import enhanced_voting as ev


def ballot_items(raw, source):
    return ev.ballot_items(raw, source)


def parse(archive_path, source, settings, output, stats, reference=None):
    ev.parse_site(archive_path, source, settings, output, stats, reference)
