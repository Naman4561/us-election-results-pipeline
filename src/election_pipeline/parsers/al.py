"""Alabama Secretary of State election night results: the statewide page and its export.

``results.html`` is the statewide results page: the election's name, "Counties
Reported", its "Last Updated" time (Central) and each contest's statewide votes by
candidate. ``export.xlsx`` is its "Export Data" workbook (``collectors.alabama``): the
"AllResults" sheet has a row per county, contest and candidate (with the candidate's
number and party), the "Statistics" sheet each county's precincts reported. Every
selected contest's county rows must add up, candidate by candidate, to the page's
statewide votes, and those to the page's contest total. Write-in votes are one
"Write-in" line. The site does not publish vote methods.
"""

import re
import zipfile

from election_pipeline import parse_report
from election_pipeline.parsers.enr_platform import page_text
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row
from election_pipeline.parsers.workbook import Workbook

HEADER = [
    "Election Code", "Election Title", "County Code", "County Name", "Contest Code",
    "Contest Title", "Candidate Number", "Candidate Name", "Votes", "Party Code",
]  # fmt: skip
STATISTICS = [
    "Election Code", "Election Title", "County Code", "Ballots Cast", "Total Precincts",
    "Precincts Reported", "Last Updated",
]  # fmt: skip
WRITE_IN = ("write-in", "Write-in")
COUNTIES = re.compile(r"Counties Reported: ([0-9]+) of ([0-9]+)")
# A candidate's name never starts with a digit: the block's total does, and ends it.
CANDIDATE = re.compile(r"([^0-9 %][^%]*?)(?: \([A-Z]{3}\))? [0-9]{1,3}\.[0-9]{2}% ([0-9,]+) ")
# A sheet of every county's contests is about 6,000 rows; allow room to grow.
MAX_ROWS = 20_000


def number(value, label):
    text = str(value).replace(",", "")
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise ValueError(f"Alabama results: invalid {label} {value!r}")
    return count(int(text))


def statewide(body, title):
    """{candidate: votes} for one contest, from its block on the page, checked against the
    block's total. The title starts a word ("GOVERNOR" is not "LIEUTENANT GOVERNOR")."""
    blocks = list(re.finditer(rf"(?<![A-Za-z]) {re.escape(title)} Percent Votes ", body))
    if len(blocks) != 1:
        raise ValueError(f"{title}: expected one statewide block on the Alabama page")
    block, position, found = body[blocks[0].end() :], 0, {}
    # Candidates follow one another ("Name (PARTY) 47.86% 806,253 "), then the total.
    while match := CANDIDATE.match(block, position):
        name = match[1].strip()
        if name in found:
            raise ValueError(f"{title}: repeated candidate on the Alabama page")
        found[name] = number(match[2], "statewide votes")
        position = match.end()
    total = re.match(r"([0-9,]+)(?: |$)", block[position:]) if found else None
    if not total or number(total[1], "contest total") != sum(found.values()):
        raise ValueError(f"{title}: Alabama page votes do not add up to its total")
    return found


def table(book, sheet, header, label, site):
    found = book.rows(sheet, max_rows=MAX_ROWS, decimals=True)
    rows = [r for r in found if any(v not in (None, "") for v in r)]
    if not rows or rows[0] != header:
        raise ValueError(f"Alabama export lacks its {label} sheet")
    for values in rows[1:]:
        if len(values) != len(header):
            raise ValueError(f"Alabama {label} row has an unexpected width")
        record = dict(zip(header, (str(v) if v is not None else "" for v in values)))
        if (record["Election Code"], record["Election Title"]) != (
            site.election_code,
            site.page_election,
        ):
            raise ValueError("Alabama export belongs to another election")
        yield record


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.platforms import enr
    from election_pipeline.states import state_adapter

    site = enr(source)
    if source.format != "bundle":
        raise ValueError("Unsupported Alabama source/election")
    year = source.election_date[:4]
    updated_at = re.compile(
        rf"Last Updated: ([0-9]{{2}}/[0-9]{{2}}/{year} "
        r"[0-9]{1,2}:[0-9]{2}:[0-9]{2} [AP]M)"
    )
    with zipfile.ZipFile(archive_path) as archive:
        body = page_text(archive.read(source.primary_filename).decode("utf-8", errors="replace"))
        book = Workbook(archive.read("export.xlsx"), settings)
    updated = updated_at.findall(body)
    reported = COUNTIES.findall(body)
    if (
        f"Export Data {site.page_election} Statewide Results" not in body
        or "results presented on these pages are unofficial" not in body
        or len(set(updated)) != 1
        or len(set(reported)) != 1
    ):
        raise ValueError("Alabama page lacks its election, status, update time or counts")
    wanted = state_adapter(source.state).races
    precincts, names = {}, {}
    for record in table(book, "Statistics", STATISTICS, "statistics", site):
        code = record["County Code"]
        done = number(record["Precincts Reported"], "precincts")
        total = number(record["Total Precincts"], "precincts")
        if code in precincts or done > total:
            raise ValueError(f"Alabama county {code}: unexpected statistics")
        precincts[code] = (done, total)
    titles, counties, candidates = {}, {}, {}
    for record in table(book, "AllResults", HEADER, "results", site):
        race = site.race(record["Contest Title"])
        if race is None:
            continue
        contest = (record["Contest Code"], record["Contest Title"])
        if race not in wanted or titles.setdefault(race, contest) != contest:
            raise ValueError(f"{race}: unexpected Alabama contest {record['Contest Title']!r}")
        code, county = record["County Code"], " ".join(record["County Name"].split())
        if names.setdefault(code, county) != county or code not in precincts:
            raise ValueError(f"Alabama county {code} is unnamed or has no statistics")
        name, party = " ".join(record["Candidate Name"].split()), record["Party Code"]
        if (name, party) == ("Write-In", "NON"):
            key, name, party = WRITE_IN[0], WRITE_IN[1], ""
        elif party in site.party_codes and name:
            key = record["Candidate Number"]
        else:
            raise ValueError(f"{race}: unexpected Alabama candidate {name!r} ({party})")
        if candidates.setdefault((race, key), (name, party)) != (name, party):
            raise ValueError(f"{race}: Alabama candidate {key} differs between counties")
        place = counties.setdefault(race, {}).setdefault(county, {})
        if key in place:
            raise ValueError(f"{race}: repeated Alabama candidate in {county}")
        place[key] = (number(record["Votes"], "votes"), code)
    if set(counties) != set(wanted) or len(names) != site.county_count:
        raise ValueError("Alabama export lacks a selected contest or a county")
    if set(counties[f"{source.state}-GOV"]) != set(names.values()):
        raise ValueError("AL-GOV: Alabama export lacks a county")
    rows = []
    for race, places in counties.items():
        page = statewide(body, titles[race][1])
        sums = {}
        for county, entries in sorted(places.items()):
            for key, (votes, code) in entries.items():
                name, party = candidates[(race, key)]
                sums[name] = sums.get(name, 0) + votes
                done, total = precincts[code]
                rows.append(row(race, race, key, name, party, votes, county, done, total))
        expected = {("Write-in" if n == "Write-In" else n): v for n, v in page.items()}
        if sums != expected:
            raise ValueError(f"{race}: Alabama counties do not add up to the statewide page")
        for (contest, key), (name, party) in candidates.items():
            if contest == race:
                rows.append(row(race, race, key, name, party, sums[name]))
        verify_scope(
            source.state,
            race,
            [n for (c, k), (n, p) in candidates.items() if c == race and k != WRITE_IN[0]],
            sorted(places),
        )
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated[0] + " CT",
        **parse_report.fields(False),
        counties_reported="{} of {}".format(*reported[0]),
        contest_total_reconciliation="passed",
    )
