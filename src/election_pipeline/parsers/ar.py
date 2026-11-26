"""Arkansas Secretary of State results (Tally): totals by county.

Arkansas elects a senator, a governor and four U.S. House members in 2026. Its federal
contests are published as contest type "Federal" and the governor as "Statewide".
Candidate names carry a party prefix ("REP Tom Cotton"), dropped where it is the
candidate's own party. The site's identity, contest types and titles are in the election
definition (``[platform]`` of ``states/AR.toml``).
"""

from election_pipeline.parsers import tally


def parse(archive_path, source, settings, output, stats, reference=None):
    tally.parse_site(archive_path, source, settings, output, stats, reference)
