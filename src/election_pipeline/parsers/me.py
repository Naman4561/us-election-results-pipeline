"""Maine Secretary of State: the general election's official tabulations, by hand.

The Secretary of State's site refuses automated clients, so the operator downloads the
tabulation spreadsheets and the District 2 ranked-choice summary and imports them,
unchanged, as one zip (``results.zip``) of:

- ``US Senate - FINAL.xlsx``, ``Governor - FINAL.xlsx`` and
  ``Rep to Congress District 1 - FINAL.xlsx``: one sheet, a row per municipality under its
  county's code, a "<code> Total" row after each county's municipalities, a
  "STATE UOCAVA" row and a statewide (or district) total row;
- ``Rep to Congress District 2.xlsx``: the same by precinct on its "Final" sheet;
- ``Nov26 CG2 RCV Summary Report 20261112_0.csv``: RCTab's District 2 summary.

A sheet's first row names its candidates ("Collins, Susan M."), its third their parties;
"Others" and a declared write-in are one "Write-in" line. Every place's votes and blanks
must add up to its ballots cast (TBC), every county's places to its total row, and the
county totals with the UOCAVA row to the sheet's total. A county's places run up to its
total row (labelled "Total", "Totals" or "TOTALS"); a place without a county code
(the Senate sheet's Woodstock/Milton Twp) may only sit among them. Counties come from their total
rows; military and overseas (UOCAVA) ballots are reported in no county, so they count
statewide only. The House counts are the ballots as marked: District 2's ranked-choice
summary must name the same ballot total and the leader's majority, and is not stored.
The spreadsheets print no report time: the operator declares it at import.
"""

import csv
import io
import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row
from election_pipeline.parsers.workbook import Workbook

MEMBERS = {
    "US Senate - FINAL.xlsx": ("ME-SEN", "US Senate", "Statewide Total"),
    "Governor - FINAL.xlsx": ("ME-GOV", "Governor", "Statewide Total"),
    "Rep to Congress District 1 - FINAL.xlsx": ("ME-01", "Sheet1", "DISTRICT 1 Total"),
    "Rep to Congress District 2.xlsx": ("ME-02", "Final", "Total"),
}
RCV_SUMMARY = "Nov26 CG2 RCV Summary Report 20261112_0.csv"
# The import zip's layout, for scripts/pack_import.py (election_pipeline.packing).
PACK = {"members": (*MEMBERS, RCV_SUMMARY)}
COUNTIES = {
    "AND": "Androscoggin", "ARO": "Aroostook", "CUM": "Cumberland", "FRA": "Franklin",
    "HAN": "Hancock", "KEN": "Kennebec", "KNO": "Knox", "LIN": "Lincoln", "OXF": "Oxford",
    "PEN": "Penobscot", "PIS": "Piscataquis", "SAG": "Sagadahoc", "SOM": "Somerset",
    "WAL": "Waldo", "WAS": "Washington", "YOR": "York",
}  # fmt: skip
PARTIES = {"Democratic": "DEM", "Republican": "REP", "Independent": "IND"}
WRITE_IN = ("write-in", "Write-in")
MAX_BYTES = 5_000_000


def number(value, label):
    if value in (None, ""):
        return 0
    if type(value) is not int:
        raise ValueError(f"Maine results: invalid {label} {value!r}")
    return count(value)


def candidate(cell, party, race):
    """(name, party) from a "Last, First" heading; a write-in for "Others" or a declared
    write-in."""
    heading = " ".join(str(cell).split())
    if heading == "Others" or heading.endswith("(Declared Write-In)"):
        return WRITE_IN[1], ""
    match = re.fullmatch(r"([^,]+), ([^,]+)", heading)
    if not match or party not in PARTIES:
        raise ValueError(f"{race}: unexpected Maine candidate heading {cell!r} ({party!r})")
    return f"{match[2]} {match[1]}", PARTIES[party]


def sheet(book, race, name, total_label):
    """(columns, {county code: votes}, uocava votes, total votes) for one tabulation, each
    votes list holding the candidate columns."""
    if name not in book.sheets:
        raise ValueError(f"{race}: Maine workbook lacks its {name!r} sheet")
    rows = [
        [" ".join(v.split()) if isinstance(v, str) else v for v in r]
        for r in book.rows(name, max_rows=2000, sums=True)
        if any(v not in (None, "") for v in r)
    ]
    headings, parties = rows[0], rows[2] + [None] * (len(rows[0]) - len(rows[2]))
    labels = next(r for r in rows[:4] if "Municipality" in r)
    place = labels.index("Municipality")
    county = labels.index("COUNTY" if "COUNTY" in labels else "CTY")
    if headings[-2:] != ["BLANK", "TBC"]:
        raise ValueError(f"{race}: Maine sheet does not end with BLANK and TBC")
    first = next(i for i, h in enumerate(headings) if i > place and h not in (None, ""))
    columns = [candidate(h, parties[i], race) for i, h in enumerate(headings[first:-2], first)]
    width = len(headings)
    counties, pending, uocava, total, uncoded = {}, [], None, None, []
    # Places start after the party row and the "Municipality" row (the first or fourth).
    for values in rows[max(rows.index(labels), 2) + 1 :]:
        values = values + [None] * (width - len(values))
        label = values[place] if isinstance(values[place], str) else ""
        if total is not None:
            if label == "%":
                continue
            raise ValueError(f"{race}: unexpected Maine row after the total")
        if len(values) != width:
            raise ValueError(f"{race}: unexpected Maine row width")
        votes = [number(v, "votes") for v in values[first:]]
        if sum(votes[:-1]) != votes[-1]:
            raise ValueError(f"{race}: Maine row {label!r} does not add up to its ballots cast")
        # A county's places run up to its total row ("AND Total", "FRA TOTALS"); a place
        # missing its county code may only sit among them, and they must add up to it.
        subtotal = re.fullmatch(r"([A-Z]{3}) (?i:totals?)", label)
        if subtotal:
            code = subtotal[1]
            codes = {c for c, _, _ in pending} - {None}
            if code not in COUNTIES or code in counties or codes != {code}:
                raise ValueError(f"{race}: unexpected Maine county total {label!r}")
            if [sum(column) for column in zip(*(v for _, _, v in pending))] != votes:
                raise ValueError(f"{race}: {code} places do not add up to the county total")
            uncoded += [f"{p} ({COUNTIES[code]})" for c, p, _ in pending if c is None]
            counties[code], pending = votes, []
        elif label == "STATE UOCAVA" and uocava is None and not pending:
            uocava = votes
        elif label == total_label and not pending:
            total = votes
        elif label and values[county] in (*COUNTIES, None) and values[county] not in counties:
            pending.append((values[county], label, votes))
        else:
            raise ValueError(f"{race}: unexpected Maine row {values[: place + 2]!r}")
    if total is None or uocava is None or pending or not counties:
        raise ValueError(f"{race}: Maine sheet lacks a total, the UOCAVA row or a county total")
    summed = [sum(column) for column in zip(uocava, *counties.values())]
    if summed != total:
        raise ValueError(f"{race}: Maine county totals and UOCAVA do not add up to the total")
    return columns, counties, uocava, total, uncoded


def ranked_choice(raw, ballots):
    """Check RCTab's District 2 summary against the spreadsheet: the same ballots, and a
    first-round leader with a majority of the active ballots (no later round decided it)."""
    lines = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    fields = {line[0]: line[1] for line in lines if len(line) > 1 and line[0]}
    if fields.get("Contest") != "Rep. to Congress" or fields.get("Jurisdiction") != "District 2":
        raise ValueError("Maine ranked-choice summary belongs to another contest")
    if fields.get("Date") != "11/3/2026" or int(fields["Total Number of Ballots"]) != ballots:
        raise ValueError("Maine ranked-choice summary does not match District 2's ballots")
    start = next(i for i, line in enumerate(lines) if line and line[0] == "Rounds")
    first = {}
    for line in lines[start + 1 :]:
        if line and line[0] == "Active Ballots":
            active = int(line[1])
            break
        if line and line[0] not in ("Eliminated", "Elected") and line[1].isdecimal():
            first[line[0]] = int(line[1])
    leader = max(first, key=first.get)
    if leader != fields.get("Winner(s)") or 2 * first[leader] <= active:
        raise ValueError("Maine ranked-choice summary: the winner had no first-round majority")
    return {"winner": leader, "first_round_votes": first[leader], "active_ballots": active}


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "ME":
        raise ValueError("Unsupported Maine source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    expected = {*MEMBERS, RCV_SUMMARY}
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if {e.filename for e in entries} != expected or len(entries) != len(expected):
            raise ValueError("Maine import is not the four tabulations and the RCV summary")
        if any(e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries):
            raise ValueError("Maine spreadsheet is too large or encrypted")
        books = {m: Workbook(bundle.read(m), settings) for m in MEMBERS}
        summary = bundle.read(RCV_SUMMARY)
    wanted = state_adapter("ME").races
    if {race for race, _, _ in MEMBERS.values()} != set(wanted):
        raise ValueError("Maine spreadsheets do not cover its races")
    rows, overseas, ballots, uncoded = [], {}, {}, {}
    for member, (race, name, total_label) in MEMBERS.items():
        columns, counties, uocava, total, places = sheet(books[member], race, name, total_label)
        if places:
            uncoded[race] = places
        if race in ("ME-SEN", "ME-GOV") and set(counties) != set(COUNTIES):
            raise ValueError(f"{race}: Maine statewide tabulation lacks a county")
        ballots[race] = total[-1]
        overseas[race] = sum(uocava[: len(columns)])
        for i, (choice, party) in enumerate(columns):
            key = WRITE_IN[0] if not party else choice
            rows.append(row(race, race, key, choice, party, total[i]))
            for code, votes in sorted(counties.items()):
                rows.append(row(race, race, key, choice, party, votes[i], COUNTIES[code]))
        names = [choice for choice, party in columns if party]
        verify_scope("ME", race, names, sorted(COUNTIES[c] for c in counties))
    tabulation = ranked_choice(summary, ballots["ME-02"])
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        contest_total_reconciliation="passed",
        ballot_totals=ballots,
        statewide_only_votes=overseas,
        ranked_choice={"ME-02": tabulation},
        places_without_county_code=uncoded,
    )
    stats["warnings"] += [
        "Military and overseas (UOCAVA) ballots are reported in no county; they count "
        "statewide only.",
        "House counts are the ballots as marked; District 2's ranked-choice tabulation "
        "elected the first-round leader.",
    ]
