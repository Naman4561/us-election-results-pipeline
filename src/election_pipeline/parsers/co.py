"""Colorado Secretary of State results (Clarity): totals by county.

``detail.zip`` is the site's "Detail XML" report: each choice's votes by county. Colorado
votes by mail and reports a single "Total Votes" type, so this is a totals-only feed.
Each county's candidate counts must add up to the statewide count, which must equal the
official summary. Governor tickets are shown as "Governor / Lieutenant Governor", as
published. The summary writes nicknames with single quotes where the detail report uses
double quotes (``'Marky Jr'`` / ``"Marky Jr"``); names are compared with quotes folded and
stored as the detail report prints them.
The site's identity, contest titles and vote type are in the election definition
(``[platform]`` of ``states/CO.toml``).
"""

from election_pipeline.parsers import clarity

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle":
        raise ValueError("Unsupported Colorado source/election")
    clarity.parse_detail_xml(archive_path, source, settings, output, stats, reference)
