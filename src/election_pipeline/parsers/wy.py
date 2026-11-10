"""Wyoming Secretary of State: the State Canvassing Board's certified results.

Wyoming has no election-night source collected here; it is added from its certified
results alone. The Secretary of State posts them as a zip of workbooks
(``2026_Wyoming_General_Results.zip``), imported unchanged as ``certified-results.zip``.
Its summaries workbook's "Statewide Candidates" sheet lists, for each statewide office,
every candidate ("Name (P)"), the write-ins, overvotes and undervotes by county, with a
"Total" row. Write-ins are kept as one "Write-in" line; over- and undervotes are votes
for no one and are left out. Every county must add up to the Total row.

Wyoming elects a senator, its at-large U.S. House member and a governor in 2026.
"""

import io
import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row
from election_pipeline.parsers.workbook import Workbook

RESULTS = "certified-results.zip"
SUMMARIES = "2026 General Results Summaries - OFFICIAL.xlsx"
SHEET = "Statewide Candidates"
TITLE = "Statewide Candidates Official Summary Wyoming General Election - November 3, 2026"
OFFICES = {
    "United States Senator": "WY-SEN",
    "United States Representative": "WY-AL",
    "Governor": "WY-GOV",
}
# The parties on Wyoming's 2026 ballot for these offices; any other is refused.
PARTIES = {"R": "REP", "D": "DEM", "L": "LIB", "C": "CON"}
COUNTIES = (
    "Albany", "Big Horn", "Campbell", "Carbon", "Converse", "Crook", "Fremont", "Goshen",
    "Hot Springs", "Johnson", "Laramie", "Lincoln", "Natrona", "Niobrara", "Park",
    "Platte", "Sheridan", "Sublette", "Sweetwater", "Teton", "Uinta", "Washakie", "Weston",
)  # fmt: skip
WRITE_IN = ("write-in", "Write-in")
NOT_VOTES = ("Overvotes", "Undervotes")


def words(value):
    return " ".join(str(value).split()) if value is not None else ""


def parse(archive_path, source, settings, output, stats, reference=None):
    raise ValueError("Wyoming is published from its certified results only")


def columns(heading, header):
    """{race: [(column, header)]} for the offices this pipeline covers."""
    starts = [(i, OFFICES.get(words(v))) for i, v in enumerate(heading) if words(v)]
    spans = {}
    for (start, race), (end, _) in zip(starts, [*starts[1:], (len(header), None)]):
        if race is not None:
            spans[race] = [(i, words(header[i])) for i in range(start, min(end, len(header)))]
    return spans


def parse_results(archive_path, name, source, settings, output, stats, reference=None):
    from election_pipeline.parsers import certified
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "WY":
        raise ValueError("Unsupported Wyoming source/election")
    with zipfile.ZipFile(io.BytesIO(certified.read(archive_path, name, settings))) as results:
        info = results.getinfo(SUMMARIES)
        if info.flag_bits & 1 or info.file_size > settings.max_uncompressed_bytes:
            raise ValueError("Wyoming summaries workbook is encrypted or too large")
        book = Workbook(results.read(info), settings)
    try:
        sheet = book.rows(SHEET)
    finally:
        book.close()
    if len(sheet) < 5 or words(sheet[0][1] if len(sheet[0]) > 1 else None) != TITLE:
        raise ValueError("Wyoming results are not the official 2026 general election summary")
    spans = columns(sheet[2], sheet[3])
    wanted = state_adapter("WY").races
    if set(spans) != set(wanted):
        raise ValueError("Wyoming results lack a selected office")
    places, total = {}, None
    for values in sheet[4:]:
        label = words(values[0] if values else None)
        if not label:
            continue
        if label in places or (total is not None):
            raise ValueError(f"Wyoming results: unexpected row {label!r}")
        if label == "Total":
            total = values
        elif label in COUNTIES:
            places[label] = values
        else:
            raise ValueError(f"Wyoming results: unknown county {label!r}")
    if total is None or set(places) != set(COUNTIES):
        raise ValueError("Wyoming results lack a county or the Total row")

    def votes(values, column):
        return count(values[column] if column < len(values) else None)

    rows = []
    for race, span in spans.items():
        choices = []
        for column, header in span:
            if header == "Write-Ins":
                choices.append((column, *WRITE_IN, ""))
            elif header in NOT_VOTES:
                continue
            else:
                match = re.fullmatch(r"(.+) \(([A-Z])\)", header)
                if not match or match[2] not in PARTIES:
                    raise ValueError(f"{race}: unexpected Wyoming candidate {header!r}")
                choices.append((column, match[1], match[1], PARTIES[match[2]]))
        headers = [h for _, h in span]
        if headers.count("Write-Ins") != 1 or any(headers.count(h) != 1 for h in NOT_VOTES):
            raise ValueError(f"{race}: unexpected Wyoming columns")
        for column, identity, choice, party in choices:
            statewide = votes(total, column)
            if sum(votes(places[p], column) for p in COUNTIES) != statewide:
                raise ValueError(f"{race} {choice}: counties do not add up to the Total row")
            rows.append(row(race, race, identity, choice, party, statewide))
            for place in COUNTIES:
                rows.append(
                    row(race, race, identity, choice, party, votes(places[place], column), place)
                )
        verify_scope("WY", race, [c[1] for c in choices if c[1] != WRITE_IN[0]], list(COUNTIES))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        certified_format="wy_results_workbook",
        contest_total_reconciliation="passed",
    )


CERTIFIED = {RESULTS: parse_results}
