"""North Dakota Secretary of State results (Tally): totals by county.

North Dakota elects only its at-large U.S. House member among the offices covered here
in 2026, published as a statewide ("SW") contest. Its site lists write-in votes as a
candidate named "write-in", kept as published.
"""

from election_pipeline.parsers import tally


def race(name):
    return "ND-AL" if name == "Representative in Congress" else None


SITE = tally.Site("ND", "North Dakota", "38", "348", "2026 General Election", race, {"ND-AL": "SW"})


def parse(archive_path, source, settings, output, stats, reference=None):
    tally.parse_site(SITE, archive_path, source, settings, output, stats, reference)
