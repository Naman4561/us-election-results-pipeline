"""Massachusetts Secretary of the Commonwealth results (PD43+): totals by city and town.

PD43+ (electionstats.state.ma.us) lists an election once its results are certified and
serves each race as a CSV by city and town. A bundle holds one CSV per race: Senate,
Governor and the nine House districts. Each starts with a header (``City/Town``, two
empty columns, the candidates, ``All Others``, ``Blanks``, ``Total Votes Cast``) and a
row of the candidates' parties, then one row per city or town and a ``TOTALS`` row.

Every town's candidate, write-in and blank counts must add up to its total votes cast,
and the towns must add up to the TOTALS row exactly. ``All Others`` (write-ins) is kept
as one "Write-in" line; blank ballots are not votes for anyone and are not stored.

Massachusetts reports by city and town, not county, so its map draws towns (Census
county subdivisions); towns are stored where other states store counties. A town split
among House districts appears in each. PD43+ abbreviates directions ("N. Adams"); names
are written out as the map writes them ("North Adams"). Counts are totals only.
"""

import csv
import io
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import formatted_count, text
from election_pipeline.parsers.structured import row as result_row
from election_pipeline.platforms import party_names

MEMBERS = {
    "senate.csv": "MA-SEN",
    "governor.csv": "MA-GOV",
    **{f"house-{n:02}.csv": f"MA-{n:02}" for n in range(1, 10)},
}
TAIL = ["All Others", "Blanks", "Total Votes Cast"]
# The parties on Massachusetts's 2026 ballot for these races; any other is refused.
PARTIES = party_names("MA")
WRITE_IN = ("write-in", "Write-in")
DIRECTIONS = {"N.": "North", "S.": "South", "E.": "East", "W.": "West"}


def town(name):
    """A city or town's name as the map writes it."""
    words = text(name, "city or town").split()
    if words[0] in DIRECTIONS:
        words[0] = DIRECTIONS[words[0]]
    return " ".join(words)


def table(raw, race):
    """(candidates [(name, party)], {town: counts}, totals) for one race's CSV."""
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    if len(rows) < 4:
        raise ValueError(f"{race}: Massachusetts results file is incomplete")
    header, parties, body, last = rows[0], rows[1], rows[2:-1], rows[-1]
    if header[:3] != ["City/Town", "", ""] or header[-3:] != TAIL:
        raise ValueError(f"{race}: unexpected Massachusetts results header")
    names = [text(n, "candidate") for n in header[3:-3]]
    labels = parties[3:]
    if (
        not names
        or len(set(names)) != len(names)
        or parties[:3] != ["", "", ""]
        or len(labels) != len(names)
        or any(p not in PARTIES for p in labels)
    ):
        raise ValueError(f"{race}: unexpected Massachusetts candidates or parties")
    width = len(header)

    def counts(line, label):
        if len(line) != width or line[1:3] != ["", ""]:
            raise ValueError(f"{label}: malformed Massachusetts results row")
        values = [formatted_count(v) for v in line[3:]]
        if sum(values[:-1]) != values[-1]:
            raise ValueError(f"{label}: votes and blanks do not add up to votes cast")
        return values

    towns = {}
    for line in body:
        place = town(line[0] if line else "")
        if place == "TOTALS" or place in towns:
            raise ValueError(f"{race}: duplicate or misplaced Massachusetts town {place}")
        towns[place] = counts(line, f"{race} {place}")
    if not towns or last[:1] != ["TOTALS"]:
        raise ValueError(f"{race}: Massachusetts results lack their TOTALS row")
    totals = counts(last, f"{race} TOTALS")
    if [sum(c[i] for c in towns.values()) for i in range(len(totals))] != totals:
        raise ValueError(f"{race}: towns do not add up to the TOTALS row")
    return list(zip(names, (PARTIES[p] for p in labels), strict=True)), towns, totals


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "MA":
        raise ValueError("Unsupported Massachusetts source/election")
    if set(MEMBERS.values()) != set(state_adapter("MA").races):
        raise ValueError("Massachusetts results lack a selected contest")
    rows = []
    with zipfile.ZipFile(archive_path) as archive:
        for member, race in MEMBERS.items():
            candidates, towns, totals = table(archive.read(member), race)
            # Candidates, then the write-in line; blanks and votes cast are not stored.
            lines = [(name, name, party) for name, party in candidates] + [
                (WRITE_IN[0], WRITE_IN[1], "")
            ]
            for index, (key, name, party) in enumerate(lines):
                rows.append(result_row(race, member, key, name, party, totals[index]))
                for place, values in towns.items():
                    rows.append(
                        result_row(race, member, key, name, party, values[index], county=place)
                    )
            verify_scope("MA", race, [name for name, _ in candidates], sorted(towns))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(contest_total_reconciliation="passed")
