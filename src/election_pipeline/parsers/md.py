"""Maryland State Board of Elections results: county breakdown pages, totals only.

For governor and each U.S. House district, the official "County Break Down" page
(``gen_detail_results_2026_...html``) lists every jurisdiction's count for every
candidate and a Totals row. Maryland elects no U.S. senator in 2026.

Checks: the page is the 2026 general election page for that contest ("Vote for 1"); the
candidate header is complete; county rows add up exactly to the Totals row, which gives
the statewide (or districtwide) counts. Each page carries its own "Last refreshed" time;
if a collection straddles a refresh, every contest is kept as of its own page and all
times are recorded. Tables may be split into several side-by-side
parts "based on the number of candidates"; each part must list the same jurisdictions.

Maryland's summary pages also split votes into Early Voting, Election Day, Mail-In and
Provisional, but provisional ballots are "NR" (not reported) until the canvass, so the
methods cannot yet add up to the totals and are not stored.

Jurisdictions are stored as "<name> County" or "Baltimore City" (both Baltimore County
and Baltimore City exist), with "Saint Mary's" written "St. Mary's" as the Census does.
"""

import re
import zipfile

from election_pipeline.parsers.html import Node, Tree
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import formatted_count, row, text
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

MEMBERS = {"MD-GOV": "governor.html", **{f"MD-0{n}": f"house-0{n}.html" for n in range(1, 9)}}
HEADINGS = {
    "MD-GOV": ("Governor / Lt. Governor",),
    **{f"MD-0{n}": ("Representative in Congress", f"District {n}") for n in range(1, 9)},
}
TITLE = "Unofficial 2026 Gubernatorial General Election Results for "


def county(name):
    if name == "Baltimore City" or name.endswith(" County"):
        return name
    if name == "Saint Mary's":
        return "St. Mary's County"
    if not re.fullmatch(r"[A-Z][A-Za-z' ]+", name):
        raise ValueError(f"Unexpected Maryland jurisdiction {name!r}")
    return name + " County"


def segments(cell):
    parts, current = [], []
    for child in cell.children:
        if isinstance(child, Node) and child.tag == "br":
            parts.append(" ".join(" ".join(current).split()))
            current = []
        else:
            current.append(child if isinstance(child, str) else child.text)
    parts.append(" ".join(" ".join(current).split()))
    return [p for p in parts if p]


def candidate(cell):
    parts = segments(cell)
    if parts == ["Other Write-Ins"]:
        return "Other Write-Ins", ""
    if len(parts) == 3 and parts[1] == "(Write-In)":
        return f"{parts[0]} (Write-In)", parts[2]
    if len(parts) != 2:
        raise ValueError("Unexpected Maryland candidate heading")
    return parts[0], parts[1]


def total_cell(cell, race):
    match = re.fullmatch(r"([0-9,]+) \([0-9.]+%\)", cell)
    if not match:
        raise ValueError(f"{race}: unexpected Maryland Totals cell {cell!r}")
    return formatted_count(match[1])


def page(raw, race):
    body = Tree(raw.decode("utf-8", errors="strict")).root
    words = " ".join(body.text.split())
    contest = HEADINGS[race]
    if TITLE + contest[0] not in words or "Vote for 1" not in words:
        raise ValueError(f"{race}: Maryland page belongs to another contest")
    if len(contest) == 2 and f"{contest[0]} {contest[1]} Vote for 1" not in words:
        raise ValueError(f"{race}: Maryland page belongs to another district")
    refreshed = re.findall(r"Last refreshed: ([0-9/]{10} [0-9:]{8} [AP]M)", words)
    reporting = re.findall(r"\(([0-9,]+) of ([0-9,]+) election day precincts reported\)", words)
    if len(set(refreshed)) != 1 or len(reporting) != 1:
        raise ValueError(f"{race}: Maryland page lacks its update time or precinct status")
    done, total = (formatted_count(v) for v in reporting[0])
    if done > total:
        raise ValueError(f"{race}: more precincts reported than exist")
    names, counties, totals = [], None, []
    for table in body.find("table", css="table-banded"):
        heads = table.find("th")
        if not heads or text(heads[0].text, "heading") != "Jurisdiction":
            raise ValueError(f"{race}: unexpected Maryland table")
        part = [candidate(h) for h in heads[1:]]
        rows = {}
        for line in table.find("tr")[1:]:
            cells = [text(c.text, "cell") for c in line.find("td")]
            if len(cells) != len(part) + 1:
                raise ValueError(f"{race}: malformed Maryland table row")
            if cells[0] == "Totals":
                totals += [total_cell(c, race) for c in cells[1:]]
                continue
            if cells[0] in rows:
                raise ValueError(f"{race}: duplicate Maryland jurisdiction")
            rows[cells[0]] = [formatted_count(c) for c in cells[1:]]
        if counties is None:
            counties = {name: [] for name in rows}
        if set(rows) != set(counties):
            raise ValueError(f"{race}: Maryland table parts list different jurisdictions")
        for name, values in rows.items():
            counties[name] += values
        names += part
    if not names or len(totals) != len(names) or not counties:
        raise ValueError(f"{race}: Maryland page lacks its Totals row")
    if len({n for n, _ in names}) != len(names):
        raise ValueError(f"{race}: duplicate Maryland candidate")
    for index, value in enumerate(totals):
        if sum(v[index] for v in counties.values()) != value:
            raise ValueError(f"{race}: county rows do not add up to the Totals row")
    return names, counties, totals, done, total, refreshed[0]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "MD":
        raise ValueError("Unsupported Maryland source/election")
    wanted = state_adapter("MD").selected_races(source)
    rows, times = [], {}
    with zipfile.ZipFile(archive_path) as archive:
        pages = {race: page(archive.read(MEMBERS[race]), race) for race in wanted}
    for race, (names, counties, totals, done, total, refreshed) in pages.items():
        times[race] = refreshed
        places = {county(name): values for name, values in counties.items()}
        if len(places) != len(counties):
            raise ValueError(f"{race}: Maryland jurisdictions collide")
        for index, (name, party) in enumerate(names):
            rows.append(
                row(race, race, name, name, party, totals[index], reporting=done, total=total)
            )
            for place, values in places.items():
                rows.append(row(race, race, name, name, party, values[index], county=place))
        verify_scope("MD", race, [n for n, _ in names], sorted(places))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    # Each page is checked on its own; a collection that straddles a refresh keeps each
    # contest as of its own page, and records every contest's time.
    stats.update(
        source_update_text="; ".join(sorted(set(times.values()))),
        source_update_times=times,
        contest_total_reconciliation="passed",
        vote_methods="reported_not_stored",
    )
