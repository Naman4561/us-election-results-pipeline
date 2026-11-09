"""Iowa Secretary of State results (Clarity): county results with vote methods.

``detail.zip`` is the site's "Detail TXT" report (Iowa publishes no detail XML). For each
contest it prints a fixed-width table: one row per county with, for each candidate,
Election Day, Absentee and Total, then the county's contest total, and a ``Totals:`` row.
Iowa's "Absentee" combines early in-person (absentee) voting and mailed ballots, so it is
stored as ``absentee_combined``.

Data rows are read as fixed 30-character columns (the report's layout). Where a county
has no votes for a candidate, Iowa sometimes leaves that candidate's Election Day and
Absentee cells blank and prints a Total of 0; only then is a blank read as 0, because two
nonnegative counts that add up to 0 must both be 0. Any other blank fails the parse.

Checks: candidate names and order match the official summary; Election Day + Absentee =
Total for every cell; each county row's candidate totals add up to its contest total; the
``Totals:`` row equals the column sums and the summary's statewide counts.

The summary lists candidates without stable IDs, so a candidate's ID is the contest key
and its position in the official list (e.g. ``1000-2``); the reviewed roster pins both.
"""

import re
import zipfile

from election_pipeline.parsers import clarity
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row, text
from election_pipeline.parsers.totals import finish, with_methods

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailtxt.zip",
}
METHODS = ("election_day", "absentee_combined")
FIELDS, load_contract, database_row = with_methods(METHODS)
TITLES = {
    "United States Senator": "IA-SEN",
    "Governor and Lt. Governor": "IA-GOV",
    **{f"United States Representative District {n}": f"IA-0{n}" for n in range(1, 5)},
}


def race(name):
    return TITLES.get(name)


def cells(line):
    return [part for part in re.split(r"\s{2,}", line.strip()) if part]


WIDTH = 30


def integer(value):
    if not re.fullmatch(r"0|[1-9][0-9]{0,11}", value):
        raise ValueError(f"Invalid Iowa count {value!r}")
    return int(value)


def columns(line, count):
    """Fixed-width cells; a value may not spill into the next column."""
    line = line.rstrip()
    if len(line) > WIDTH * count:
        raise ValueError("Iowa row is wider than its table")
    values = [line[i : i + WIDTH] for i in range(0, WIDTH * count, WIDTH)]
    if any(v.strip() and (v[0] == " " or "  " in v.strip()) for v in values):
        raise ValueError("Iowa cell is not aligned to its column")
    return [v.strip() for v in values]


def group(day, absentee, total):
    if not total:
        raise ValueError("Iowa candidate total is blank")
    if not day and not absentee and total == "0":
        return 0, 0, 0
    return integer(day), integer(absentee), integer(total)


def blocks(report):
    """{title: (candidate names, [(county, [(day, absentee, total), ...], contest total)])}."""
    lines = report.decode("utf-8-sig").splitlines()
    found, index = {}, 0
    while index < len(lines):
        match = re.fullmatch(r"(.+?) \( ?([0-9]+)\)", lines[index].strip())
        if not match or match[1] not in TITLES:
            index += 1
            continue
        title = match[1]
        if title in found or match[2] != "1":
            raise ValueError(f"{title}: duplicate or multi-seat Iowa contest")
        if index + 2 >= len(lines):
            raise ValueError(f"{title}: Iowa contest heading has no table")
        names = cells(lines[index + 1])
        header = cells(lines[index + 2])
        if header != ["County", "Registered Voters"] + ["Election Day", "Absentee", "Total"] * len(
            names
        ) + ["Total"]:
            raise ValueError(f"{title}: unexpected Iowa column layout")
        counties, index = [], index + 3
        while True:
            if index >= len(lines):
                raise ValueError(f"{title}: Iowa table has no Totals row")
            values = columns(lines[index], 3 + 3 * len(names))
            index += 1
            if not values[0]:
                raise ValueError(f"{title}: malformed Iowa row")
            numbers = [integer(values[1]), integer(values[-1])]
            groups = [group(*values[2 + 3 * n : 5 + 3 * n]) for n in range(len(names))]
            for day, absentee, total in groups:
                if day + absentee != total:
                    raise ValueError(f"{title}: Election Day + Absentee differs from Total")
            if sum(g[2] for g in groups) != numbers[-1]:
                raise ValueError(f"{title}: candidate totals differ from the contest total")
            if values[0] == "Totals:":
                for n in range(len(names)):
                    for k in range(3):
                        if sum(c[1][n][k] for c in counties) != groups[n][k]:
                            raise ValueError(f"{title}: county rows do not add up to Totals")
                break
            counties.append((text(values[0], "county"), groups, numbers[-1]))
        found[title] = (names, counties)
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "IA":
        raise ValueError("Unsupported Iowa source/election")
    wanted = state_adapter("IA").races
    with zipfile.ZipFile(archive_path) as archive:
        updated = clarity.settings_check(archive.read("settings.json"), "IA", settings)
        official = clarity.summary(archive.read("summary.json"), race, settings)
        report = blocks(clarity.detail_member(archive.read("detail.zip"), settings, "txt"))
    if set(official) != set(wanted):
        raise ValueError("Iowa summary lacks a selected contest")
    tables = {TITLES[title]: table for title, table in report.items() if TITLES[title] in wanted}
    if set(tables) != set(wanted):
        raise ValueError("Iowa detail report lacks a selected contest")
    rows = []
    for identity, (key, expected) in official.items():
        names, counties = tables[identity]
        if names != [name for name, _, _ in expected]:
            raise ValueError(f"{identity}: detail and summary candidates differ")
        candidates = [f"{key}-{n}" for n in range(1, len(expected) + 1)]
        seen = set()
        for county, _, _ in counties:
            if county in seen:
                raise ValueError(f"{identity}: duplicate county {county}")
            seen.add(county)
        for n, (name, party, votes) in enumerate(expected):
            statewide = [sum(c[1][n][k] for c in counties) for k in range(3)]
            if statewide[2] != votes:
                raise ValueError(f"{identity}: detail total differs from the official summary")
            rows.append(
                {
                    **row(identity, key, candidates[n], name, party, votes),
                    "election_day": statewide[0],
                    "absentee_combined": statewide[1],
                }
            )
            for county, groups, _ in counties:
                day, absentee, total = groups[n]
                rows.append(
                    {
                        **row(identity, key, candidates[n], name, party, total, county=county),
                        "election_day": day,
                        "absentee_combined": absentee,
                    }
                )
        verify_scope("IA", identity, candidates, sorted(seen))
    finish(
        rows, source, settings, output, stats, reference=reference, reconcile=True, methods=METHODS
    )
    stats.update(
        source_update_text=updated,
        contest_total_reconciliation="passed",
        vote_methods="stored",
    )
