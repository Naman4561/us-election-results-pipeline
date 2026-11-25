"""Wisconsin: each county's own results report, assembled into one zip as counties come in.

The Wisconsin Elections Commission posts results only once it certifies, about December
1; each of the 72 county clerks posts its own before then, in its own layout. The
import, ``results.zip``, holds ``manifest.json`` and each collected county's report
unchanged. The manifest names, for each county, its report file and that file's SHA-256,
where it was posted, when it was saved and the report's own time (as printed, or its
PDF's creation time; a results page may give none), whether it is the county's canvass
(``canvassed``) or election-night results (``unofficial``), and how it is read:

- ``summary_report``: an "Election Summary Report" (Door, Ozaukee, Walworth); each
  contest's lines add up to its "Total Votes", and an "Unresolved Write-In" count is a
  write-in unless the contest lists its write-ins itself (then it restates them).
- ``summary_list``: a summary listing each contest's lines after its precinct count,
  every precinct reported (Chippewa).
- ``ess_summary``: an ES&S Summary Results Report (Douglas, La Crosse, Lincoln); each
  contest's lines add up to its "Total Votes Cast" where printed, every precinct reported.
- ``canvass_statement``: the canvass's summary statement, each contest's "total number
  of votes cast" and what each line "received", which must add up to it (Green).
- ``table_page``: a results page's table of candidate, total, office and each reporting
  unit (Oneida); the units add up to each total.
- ``review``: a report no reader takes (a scan, or a one-off layout). Its counts are
  transcribed into ``<County>.review.json`` for the report's SHA-256 and signed by the
  operator who checked them against it; where the report prints a contest total, the
  counts must add up to it.

The reviewed table ``resources/wi_2026_contests.json`` names every race's ballot
candidates, their parties and the spellings counties print. Each county must report
the Governor and every House district the reviewed roster places it in, and nothing
else; in each, every ballot candidate is found once, and every other line is a write-in,
kept as one "Write-in" line. Statewide votes are the collected counties' sums
(``State.collected_counties``). The newest county report time is the import's report
time.
"""

import hashlib
import io
import json
import logging
import re
import zipfile
from datetime import datetime
from html import unescape
from importlib.resources import files

from pypdf import PdfReader

from election_pipeline import reviews
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.parsers.structured import count, row

WRITE_IN = ("write-in", "Write-in")
MAX_BYTES = 30_000_000
# The import zip's layout, for scripts/pack_import.py (election_pipeline.packing).
PACK = {"manifest": "manifest.json", "section": "counties", "review": "{key}.review.json"}
MAX_PAGES = 100
COUNT = r"[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+"
GOVERNOR = re.compile(r"(?i)^governor\b")
CONGRESS = re.compile(
    r"(?i)^rep(?:resentative|\.)? in congress\W*(?:dist(?:rict|\.)?\s*#?|d)\s*([1-8])\b"
)
WRITTEN = re.compile(r"(?i)write[- ]?in|scattering")
REPORTS = ("canvassed", "unofficial")
FORMATS = (
    "summary_report",
    "summary_list",
    "ess_summary",
    "canvass_statement",
    "table_page",
    "review",
)


def table():
    raw = files("election_pipeline").joinpath("resources/wi_2026_contests.json").read_text("utf-8")
    return json.loads(raw)


def key(name):
    """A spelling's comparison key: a trailing party or note in parentheses and party
    marks dropped (a wrapped name may print its party mid-name), letters only, upper case."""
    name = re.sub(r"\s*\([^)]*\)$", "", " ".join(str(name).split()))
    name = re.sub(r"(?:^|\s)(?:REP|DEM|IND|LIB|GRN|WGR|CON|NP|Rep\.|Dem\.|Ind\.)(?=\s|$)", "", name)
    return re.sub(r"[^A-Z]", "", name.upper())


def number(text, label):
    if not re.fullmatch(COUNT, text):
        raise ValueError(f"Wisconsin results: invalid {label} {text!r}")
    return count(int(text.replace(",", "")))


def race_of(title):
    """The race a contest title names (Governor, or a House district), or None."""
    title = " ".join(title.split())
    if GOVERNOR.match(title):
        return "WI-GOV"
    match = CONGRESS.match(title)
    return f"WI-0{match[1]}" if match else None


class Race:
    """One race's ballot candidates: {spelling key: (name, party)}."""

    def __init__(self, race, entry):
        self.race, self.names = race, {}
        for candidate in entry["candidates"]:
            for spelling in [candidate["name"], *candidate.get("spellings", [])]:
                if self.names.get(key(spelling), (candidate["name"],))[0] != candidate["name"]:
                    raise ValueError(f"{race}: spelling {spelling!r} names two candidates")
                self.names[key(spelling)] = (candidate["name"], candidate["party"])
        self.ballot = {name for name, _ in self.names.values()}
        self.party = dict(self.names.values())

    def split(self, lines, label):
        """{name: votes} for the ballot candidates and the write-in total from a
        contest's (printed name, votes) lines, each ballot candidate found once."""
        found, write_in = {}, 0
        for printed, votes in lines:
            candidate = None if WRITTEN.search(printed) else self.names.get(key(printed))
            if candidate is None:
                write_in += votes
                continue
            if candidate[0] in found:
                raise ValueError(f"{self.race} {label}: {candidate[0]} printed twice")
            found[candidate[0]] = votes
        if set(found) != self.ballot:
            missing = sorted(self.ballot - set(found))
            raise ValueError(f"{self.race} {label}: ballot candidate missing: {missing}")
        return found, write_in


def pdf_lines(raw, label):
    """The PDF's lines (layout text, spaces collapsed, blank lines dropped)."""
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError(f"{label}: PDF is encrypted or has an unexpected page count")
    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    finally:
        logger.setLevel(level)
    lines = [" ".join(line.split()) for page in pages for line in page.splitlines()]
    return [line for line in lines if line]


def once(found, race, lines, label):
    if race in found:
        raise ValueError(f"{label} {race}: contest printed twice")
    found[race] = lines


def summary_report(raw, label):
    """{race: lines} from an Election Summary Report. A contest's lines run from its
    "Candidate" heading to its "Total Votes", which they must add up to; a name too long
    for its column continues on the next line. An "Unresolved Write-In" count after the
    total is a write-in, unless the contest lists its write-ins itself."""
    lines = pdf_lines(raw, label)
    if not any(line == "Election Summary Report" for line in lines[:12]) and not any(
        "Summary for: All Contests" in line or "UNOFFICIAL" in line for line in lines[:12]
    ):
        raise ValueError(f"{label}: not an Election Summary Report")
    found = {}
    index = 0
    while index < len(lines):
        title = re.sub(r"\s*\(Vote for \d+\)$", "", lines[index])
        race = race_of(title) if lines[index].endswith(")") else None
        index += 1
        if race is None:
            continue
        while index < len(lines) and not lines[index].startswith("Candidate "):
            index += 1
        block, total = [], None
        for index in range(index + 1, len(lines)):
            line = lines[index]
            if line.startswith("Total Votes "):
                total = number(line.removeprefix("Total Votes "), "total")
                break
            match = re.fullmatch(rf"(.+?)\s+({COUNT})(?:\s+[0-9.]+%)?", line)
            if match:
                block.append([match[1], number(match[2], "votes")])
            elif block and not re.search(r"[0-9]", line):
                block[-1][0] += " " + line
            else:
                raise ValueError(f"{label} {race}: unreadable line {line!r}")
        rest = lines[index + 1 : index + 3]
        unresolved = [
            number(m[1], "write-ins")
            for m in (re.fullmatch(rf"Unresolved Write-In ({COUNT})", x) for x in rest)
            if m
        ]
        named = sum(v for n, v in block if not WRITTEN.search(n))
        written = sum(v for n, v in block if WRITTEN.search(n))
        if any(WRITTEN.search(n) for n, _ in block):
            # A contest that lists its write-ins (in its Total Votes or not: Washington,
            # Walworth) restates them after as "Unresolved Write-In", counted once.
            if total not in (named + written, named) or unresolved not in ([], [written]):
                raise ValueError(f"{label} {race}: lines do not add up to Total Votes")
        else:
            if total != named:
                raise ValueError(f"{label} {race}: lines do not add up to Total Votes")
            block += [["Unresolved Write-In", unresolved[0]]] if unresolved else []
        once(found, race, [tuple(x) for x in block], label)
    return found


def summary_list(raw, label):
    """{race: lines} from a summary listing each contest's title, then (for a contest not
    on every ballot) "Precincts: N Reported: N", then its lines, every precinct reported
    (the countywide count heads the summary). A contest ends at the next title: a line
    with no count, or one followed by a precinct count. Precinct pages follow the
    summary; the first listing of each contest is the summary's."""
    lines = pdf_lines(raw, label)
    overall = [
        re.fullmatch(r"Number of Precincts: (\d+) Number of Precincts Reporting: (\d+)", x)
        for x in lines[:12]
    ]
    overall = [m for m in overall if m]
    if len(overall) != 1 or overall[0][1] != overall[0][2]:
        raise ValueError(f"{label}: not a summary of every precinct")
    found = {}
    for index, line in enumerate(lines[:-1]):
        race = race_of(line)
        if race is None or race in found:
            continue
        start = index + 1
        precincts = re.fullmatch(r"Precincts: (\d+) Reported: (\d+)", lines[start])
        if precincts:
            if precincts[1] != precincts[2]:
                raise ValueError(f"{label} {race}: not every precinct reported")
            start += 1
        block = []
        for at in range(start, len(lines)):
            match = re.fullmatch(rf"(.+?)\s+({COUNT})", lines[at])
            following = lines[at + 1] if at + 1 < len(lines) else ""
            if not match or following.startswith("Precincts:"):
                break
            block.append((match[1], number(match[2], "votes")))
        found[race] = block
    return found


def ess_summary(raw, label):
    """{race: lines} from an ES&S Summary Results Report (Douglas, La Crosse, Lincoln):
    each contest's title, "Vote For 1" and TOTAL heading, then its lines (TOTAL is the
    first count; other columns follow) to "Write-In Totals". Where the report prints
    "Total Votes Cast", the lines must add up to it; where it prints "Precincts
    Reporting", every precinct must have reported."""
    lines = pdf_lines(raw, label)
    if not any("Summary Results Report" in line for line in lines[:6]):
        raise ValueError(f"{label}: not a Summary Results Report")
    found = {}
    for index, line in enumerate(lines[:-2]):
        race = race_of(line)
        if race is None or lines[index + 1] != "Vote For 1":
            continue
        if not lines[index + 2].startswith("TOTAL"):
            raise ValueError(f"{label} {race}: unexpected contest heading")
        block, total, written = [], None, False
        for line in lines[index + 3 :]:
            if line == "Day":
                continue
            if line.startswith("Total Votes Cast"):
                total = number(re.search(COUNT, line[16:])[0], "total")
                continue
            reporting = re.fullmatch(r"Precincts Reporting (\d+) of (\d+)", line)
            if reporting and reporting[1] != reporting[2]:
                raise ValueError(f"{label} {race}: not every precinct reported")
            if written or re.match(r"(Overvotes|Undervotes|Contest Totals|Precincts)", line):
                if reporting or not re.match(r"(Overvotes|Undervotes|Contest Totals)", line):
                    break
                continue
            match = re.fullmatch(rf"(.+?) ({COUNT})(?: [0-9.]+%)?(?: .*)?", line)
            if not match or match[1] == "Not Assigned":
                raise ValueError(f"{label} {race}: unreadable line {line!r}")
            block.append((match[1], number(match[2], "votes")))
            written = match[1] == "Write-In Totals"
        if not written or (total is not None and sum(v for _, v in block) != total):
            raise ValueError(f"{label} {race}: lines do not add up to Total Votes Cast")
        once(found, race, block, label)
    return found


def canvass_statement(raw, label):
    """{race: lines} from the canvass summary statement ("The total number of votes cast
    for the <contest> was N ... of which <name> received N"); each contest's lines must
    add up to its N."""
    text = "\n".join(pdf_lines(raw, label))
    found = {}
    # A statement's title and total may share a line or not (Calumet prints "was N" on
    # its own line, and its first line "of which <name> received N").
    for statement in re.split(r"(?=The total number of votes cast)", text)[1:]:
        heading = rf"The total number of votes cast (?:for the )?(.+?)\s+was\s+({COUNT})\b"
        match = re.match(heading, statement, re.S)
        race = match and race_of(" ".join(match[1].split()))
        if not race:
            continue
        block = []
        for line in statement[match.end() :].splitlines():
            received = re.fullmatch(rf"(?:of which )?(.+?) received ({COUNT})", line.strip())
            if received:
                block.append((received[1], number(received[2], "votes")))
        if not block or sum(v for _, v in block) != number(match[2], "total"):
            raise ValueError(f"{label} {race}: lines do not add up to the votes cast")
        once(found, race, block, label)
    return found


def table_page(raw, label):
    """{race: lines} from a results page's table rows: candidate, total, office, and each
    reporting unit's votes, which must add up to the total."""
    html = raw.decode("utf-8")
    found = {}
    for body in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", html):
        cells = [
            " ".join(unescape(re.sub(r"<[^>]+>", " ", cell)).split())
            for cell in re.findall(r"(?s)<td[^>]*>(.*?)</td>", body)
        ]
        if len(cells) < 4:
            continue
        race = race_of(re.sub(r"^\d+\)\s*", "", cells[2]))
        if race is None:
            continue
        total = number(cells[1], "total")
        if sum(number(c, "unit votes") for c in cells[3:]) != total:
            raise ValueError(f"{label} {race}: reporting units do not add up to {cells[0]}")
        found.setdefault(race, []).append((cells[0], total))
    return found


def review(document, sha256s, label):
    """{race: lines} from a signed review of a report's counts; a report posted in parts
    names every part's SHA-256, in order."""
    return {race: lines for race, (lines, _) in reviews.read(document, sha256s, label).items()}


READERS = {
    "summary_report": summary_report,
    "summary_list": summary_list,
    "ess_summary": ess_summary,
    "canvass_statement": canvass_statement,
}


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "WI":
        raise ValueError("Unsupported Wisconsin source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    reviewed = table()
    adapter = state_adapter("WI")
    if set(reviewed["races"]) != set(adapter.races):
        raise ValueError("Wisconsin's reviewed table does not cover its races")
    # A race whose ballot is not reviewed yet takes no county.
    races = {race: Race(race, e) for race, e in reviewed["races"].items() if e["candidates"]}
    roster = ROSTERS["WI"]
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if len(entries) > 150 or any(e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries):
            raise ValueError("Wisconsin import has too many, too large or encrypted files")
        manifest = json.loads(bundle.read("manifest.json"))["counties"]
        expected = {"manifest.json"}
        results, times, kinds = {}, [], {}
        for county, entry in sorted(manifest.items()):
            label = f"{county} County"
            if county not in roster["WI-GOV"]["counties"]:
                raise ValueError(f"Wisconsin has no county {county!r}")
            if entry["report"] not in REPORTS or entry["format"] not in FORMATS:
                raise ValueError(f"{label}: unknown report kind or format")
            # A county that posts its races in separate reports (Vilas) lists them as parts,
            # which only a review can read.
            parts = entry.get("parts")
            if parts is None:
                parts = [{"file": entry["file"], "sha256": entry["sha256"]}]
            elif "file" in entry or len(parts) < 2 or entry["format"] != "review":
                raise ValueError(f"{label}: a report in parts is two or more files and a review")
            for part in parts:
                data = bundle.read(part["file"])
                if hashlib.sha256(data).hexdigest() != part["sha256"]:
                    raise ValueError(f"{label}: report differs from its listed SHA-256")
                expected.add(part["file"])
            # The time the report prints, or its PDF's creation time; a page may give none.
            reported = entry["report_time"] and datetime.fromisoformat(entry["report_time"])
            saved = datetime.fromisoformat(entry["saved_at"])
            if saved.tzinfo is None or reported and (reported.tzinfo is None or reported > saved):
                raise ValueError(f"{label}: report time is missing a timezone or follows saving")
            if entry["format"] == "review":
                name = f"{county}.review.json"
                expected.add(name)
                document = json.loads(bundle.read(name))
                if reviews.place(document) != county:
                    raise ValueError(f"{label}: review names another county")
                found = review(document, [part["sha256"] for part in parts], label)
            elif entry["format"] == "table_page":
                found = table_page(data, label)
            else:
                found = READERS[entry["format"]](data, label)
            wanted = {race for race in adapter.races if county in roster[race]["counties"]}
            if wanted - set(races):
                raise ValueError(f"{label}: {sorted(wanted - set(races))} not yet reviewed")
            if set(found) != wanted:
                raise ValueError(
                    f"{label}: reports {sorted(found)}, but its races are {sorted(wanted)}"
                )
            results[county], kinds[county] = found, entry["report"]
            times += [reported] if reported else []
        if {e.filename for e in entries} != expected:
            raise ValueError("Wisconsin import's files do not match its manifest")
    if not results or not times:
        raise ValueError("Wisconsin import has no county, or no county report time")
    rows, statewide, places = [], {}, {}
    for county, by_race in sorted(results.items()):
        for race, lines in sorted(by_race.items()):
            found, write_in = races[race].split(lines, f"{county} County")
            totals = statewide.setdefault(race, {})
            for candidate, votes in sorted(found.items()):
                party = races[race].party[candidate]
                rows.append(row(race, race, candidate, candidate, party, votes, county))
                totals[candidate] = totals.get(candidate, 0) + votes
            rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_in, county))
            totals[WRITE_IN[1]] = totals.get(WRITE_IN[1], 0) + write_in
            places.setdefault(race, []).append(county)
    for race, totals in sorted(statewide.items()):
        for candidate, votes in totals.items():
            if candidate == WRITE_IN[1]:
                rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", votes))
                continue
            rows.append(row(race, race, candidate, candidate, races[race].party[candidate], votes))
        verify_scope("WI", race, sorted(races[race].ballot), sorted(places[race]), collected=True)
    finish(
        rows, source, settings, output, stats, reference=reference, reconcile=True,
        races=sorted(statewide),
    )  # fmt: skip
    canvassed = sum(kind == "canvassed" for kind in kinds.values())
    stats.update(
        source_generated_at=max(times).isoformat(),
        source_update_text=(
            f"{len(results)} of 72 counties ({canvassed} canvassed, "
            f"{len(results) - canvassed} unofficial); newest county report {max(times).isoformat()}"
        ),
        official_results=False,
        contest_total_reconciliation="passed",
        county_reports={
            county: {k: manifest[county][k] for k in ("report", "format", "url", "report_time")}
            for county in sorted(results)
        },
    )
