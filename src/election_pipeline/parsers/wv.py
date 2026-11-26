"""West Virginia Secretary of State results (Clarity): totals by county.

``detail.zip`` is the site's "Detail XML" report: each choice's votes by county. West
Virginia's report carries one vote type, "Election Day", holding each choice's full count
(absentee and early ballots are not split out), so this is a totals-only feed. Each
county's candidate counts must add up to the statewide count, which must equal the
official summary. Names are stored as published, in capitals.
The site's identity, contest titles and vote type are in the election definition
(``[platform]`` of ``states/WV.toml``).
"""

from election_pipeline.parsers import clarity

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle":
        raise ValueError("Unsupported West Virginia source/election")
    clarity.parse_detail_xml(archive_path, source, settings, output, stats, reference)
