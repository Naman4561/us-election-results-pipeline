"""Colorado Secretary of State results (Clarity): totals by county.

``detail.zip`` is the site's "Detail XML" report: each choice's votes by county. Colorado
votes by mail and reports a single "Total Votes" type, so this is a totals-only feed.
Each county's candidate counts must add up to the statewide count, which must equal the
official summary. Governor tickets are shown as "Governor / Lieutenant Governor", as
published. The summary writes nicknames with single quotes where the detail report uses
double quotes (``'Marky Jr'`` / ``"Marky Jr"``); names are compared with quotes folded and
stored as the detail report prints them.
"""

import re

from election_pipeline.parsers import clarity
from election_pipeline.parsers.totals import database_row, load_contract  # noqa: F401

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}


def race(name):
    if name == "United States Senator":
        return "CO-SEN"
    if name == "Governor / Lieutenant Governor":
        return "CO-GOV"
    match = re.fullmatch(
        r"Representative to the 120th United States Congress - District ([1-8])", name
    )
    return f"CO-0{match[1]}" if match else None


def same_name(detail, summary):
    def fold(name):
        return name.replace("''", '"').replace("'", '"')

    return fold(detail) == fold(summary)


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "CO":
        raise ValueError("Unsupported Colorado source/election")
    clarity.parse_detail_xml(
        archive_path,
        source,
        settings,
        output,
        stats,
        reference,
        state="Colorado",
        race=race,
        vote_type="Total Votes",
        same_name=same_name,
    )
