"""West Virginia Secretary of State results (Clarity): totals by county.

``detail.zip`` is the site's "Detail XML" report: each choice's votes by county. West
Virginia's report carries one vote type, "Election Day", holding each choice's full count
(absentee and early ballots are not split out), so this is a totals-only feed. Each
county's candidate counts must add up to the statewide count, which must equal the
official summary. Names are stored as published, in capitals.
"""

import re

from election_pipeline.parsers import clarity

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}


def race(name):
    if name == "U.S. SENATOR":
        return "WV-SEN"
    match = re.fullmatch(
        r"U\.S\. HOUSE OF REPRESENTATIVES, ([12])(?:st|nd) Congressional District", name
    )
    return f"WV-0{match[1]}" if match else None


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "WV":
        raise ValueError("Unsupported West Virginia source/election")
    clarity.parse_detail_xml(
        archive_path,
        source,
        settings,
        output,
        stats,
        reference,
        state="West Virginia",
        race=race,
        vote_type="Election Day",
        same_name=str.__eq__,
    )
