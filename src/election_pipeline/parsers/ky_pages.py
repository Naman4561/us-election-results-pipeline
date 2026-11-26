"""Kentucky State Board of Elections: its results pages, saved by hand.

The live results feed (parsers/ky.py) and its data files refuse automated clients since
election week, so the operator saves, from the browser, the Statewide results page with
one race's "County Results" view open, once for each race (as a single-file web page,
MHTML), and imports the seven pages unchanged as one zip (``results.zip``; members
``KY-SEN.mhtml`` and ``KY-01.mhtml`` to ``KY-06.mhtml``).

Each page holds every contest's statewide totals and its race's votes by county. Every
page must be the same update of the same election; each race's counties must add up,
candidate by candidate, to its statewide totals; and its counties must be the reviewed
ones. Party IDs are the feed's own party list (captured Nov 3, 2026). The page's "Last
Update" time is the report time. The pages give totals only.
"""

import email
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape

from election_pipeline import parse_report
from election_pipeline.parsers.ky import contest_key
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import formatted_count, row
from election_pipeline.platforms import party_names

PAGE = "https://vrsws.sos.ky.gov/liveresults/Statewide"
# The feed's PoliticalParties list (PartyId -> Abbreviation), from its Nov 3 capture.
PARTIES = party_names("KY")
WRITE_IN = ("write-in", "Write-in")
EASTERN = timezone(timedelta(hours=-5))
MAX_BYTES = 40_000_000
HEAD = "<!--Table Heading Container-->"
ROW = re.compile(
    r'<div class="divTableRow resultTableRow(?: collapsible)?"(?: style="[^"]*")? '
    r'partyid="(\d*)">\s*<div class="divTableCell candidateCell">\s*([^<]*?)\s*</div>'
    r'(?:(?!resultTableRow).)*?<div class="divTableCell dataCell">\s*([\d,]+) - \d+% of '
    r"reported votes\s*</div>",
    re.S,
)


def display(name):
    """A ballot name ("Morgan McGARVEY") as the name is written ("Morgan McGarvey")."""
    name = " ".join(unescape(name).split())
    return re.sub(r"([A-Z])([A-Z]+)", lambda m: m[1] + m[2].lower(), name)


def lines(html, where):
    """A results table's (party ID, ballot name, votes) rows, every row accounted for."""
    found = [
        (party, unescape(name), formatted_count(votes)) for party, name, votes in ROW.findall(html)
    ]
    if not found or len(found) != html.count("resultTableRow"):
        raise ValueError(f"Kentucky {where}: unreadable results rows")
    return found


def page(raw, member):
    """The saved Statewide page's HTML."""
    message = email.message_from_bytes(raw)
    pages = [
        part
        for part in message.walk()
        if part.get_content_type() == "text/html" and part["Content-Location"] == PAGE
    ]
    if len(pages) != 1:
        raise ValueError(f"Kentucky {member} is not a saved Statewide results page")
    return pages[0].get_payload(decode=True).decode(pages[0].get_content_charset() or "utf-8")


def read(html, member):
    """(race, update time, reporting text, statewide rows, {county: rows}) of one page."""
    if (
        "ALL RESULTS ARE UNOFFICIAL" not in html
        or "November 03, 2026" not in html
        or ">2026 General Election" not in html
    ):
        raise ValueError(f"Kentucky {member} belongs to another election or status")
    updates = set(re.findall(r"Last Update: ([0-9/]+ [0-9:]+ [AP]M)", html))
    reporting = set(re.findall(r"Counties Reporting: (\d+ / \d+)", html))
    if len(updates) != 1 or len(reporting) != 1:
        raise ValueError(f"Kentucky {member} lacks one update time")
    start = html.find('id="myModal"')
    if start < 0 or html.find(HEAD) < start:
        raise ValueError(f"Kentucky {member} has no county results open")
    modal, rest = html[start : html.find(HEAD)], html[html.find(HEAD) :]
    titles = re.findall(r"<h3[^>]*>(.*?)</h3>", modal, re.S)
    if len(titles) != 1:
        raise ValueError(f"Kentucky {member} has no single race's county results")
    title = " ".join(unescape(titles[0]).split())
    race = contest_key(title)
    if race is None or f"{race}.mhtml" != member:
        raise ValueError(f"Kentucky {member} holds {title!r}")
    counties = {}
    for chunk in modal.split('<div class="divTableHeadCell">')[1:]:
        county = " ".join(unescape(chunk[: chunk.find("<")]).split())
        if not county or county in counties:
            raise ValueError(f"{race}: missing or repeated Kentucky county {county!r}")
        counties[county] = lines(chunk, f"{race} {county}")
    statewide = []
    for block in rest.split(HEAD)[1:]:
        heading = re.search(r'divTableHeadCell" style="width:60%">(.*?)<span', block, re.S)
        if heading and contest_key(" ".join(unescape(heading[1]).split())) == race:
            statewide.append(lines(block, f"{race} statewide"))
    if len(statewide) != 1:
        raise ValueError(f"{race}: Kentucky statewide totals missing or repeated")
    return race, updates.pop(), reporting.pop(), statewide[0], counties


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "KY":
        raise ValueError("Unsupported Kentucky source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    wanted = state_adapter("KY").races
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if {e.filename for e in entries} != {f"{race}.mhtml" for race in wanted} or any(
            e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries
        ):
            raise ValueError("Kentucky import needs one saved page per race, and only those")
        pages = [read(page(bundle.read(e.filename), e.filename), e.filename) for e in entries]
    if len({(update, reporting) for _, update, reporting, _, _ in pages}) != 1:
        raise ValueError("Kentucky pages are from different updates; save them again together")
    rows = []
    for race, _, _, statewide, counties in sorted(pages):
        choices = {}
        for party, name, _ in statewide:
            if name == "Write-In" and party == "":
                choices[name] = (*WRITE_IN, "")
            elif party in PARTIES and name not in choices:
                choices[name] = (name, display(name), PARTIES[party])
            else:
                raise ValueError(f"{race}: unexpected Kentucky candidate {name!r}")
        sums = dict.fromkeys(choices, 0)
        for county, found in sorted(counties.items()):
            # Each table lists its leader first, so the order differs by county.
            listed = [(p, n) for p, n, _ in found]
            if len(listed) != len(set(listed)) or set(listed) != {(p, n) for p, n, _ in statewide}:
                raise ValueError(f"{race}: {county} lists other candidates")
            for _, name, votes in found:
                key, shown, party = choices[name]
                sums[name] += votes
                rows.append(row(race, race, key, shown, party, votes, county))
        if sums != {name: votes for _, name, votes in statewide}:
            raise ValueError(f"{race}: Kentucky counties do not add up to the statewide totals")
        for _, name, votes in statewide:
            key, shown, party = choices[name]
            rows.append(row(race, race, key, shown, party, votes))
        names = [shown for key, shown, _ in choices.values() if key != WRITE_IN[0]]
        verify_scope("KY", race, names, sorted(counties))
    _, update, reporting, _, _ = pages[0]
    generated = datetime.strptime(update, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=EASTERN)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=f"Last Update: {update}; counties reporting {reporting}",
        source_generated_at=generated.isoformat(),
        # Saved results pages carry no official marker; certification is attested.
        **parse_report.fields(parse_report.NOT_STATED),
        contest_total_reconciliation="passed",
    )
