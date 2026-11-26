"""North Dakota Secretary of State results (Tally): totals by county.

North Dakota elects only its at-large U.S. House member among the offices covered here
in 2026, published as a statewide ("SW") contest. Its site lists write-in votes as a
candidate named "write-in", kept as published. The site's identity and contest title are
in the election definition (``[platform]`` of ``states/ND.toml``).
"""

from election_pipeline.parsers import tally


def parse(archive_path, source, settings, output, stats, reference=None):
    tally.parse_site(archive_path, source, settings, output, stats, reference)
