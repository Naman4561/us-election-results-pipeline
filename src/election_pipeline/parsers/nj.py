"""New Jersey: its 21 county clerks' results, assembled into one zip by hand.

New Jersey publishes no statewide results before it certifies; each county clerk posts
its own. ``scripts/nj_bundle.py`` assembles them into ``results.zip``: each Clarity
county's ``summary.json`` and ``electionsettings.json``; Camden's, Hunterdon's,
Sussex's and Warren's ES&S "Summary Results Report" PDFs; Salem's results page; and
Bergen's results summary as printed from its site; with ``sources.json`` naming each
member's address and SHA-256.

The reviewed table ``resources/nj_2026_contests.json`` names, for every county, the
title of each selected contest there, and for every race its ballot candidates (one
party each; New Jersey's minor "parties" are ballot slogans and stand as IND) and the
spellings counties print them in. In each county every ballot candidate of each of its
races must be found once; every other line of a contest is a write-in, kept as one
"Write-in" line (Burlington lists each written-in name). Each county's own check: a
Clarity contest's candidates add up to its total, an ES&S contest's candidates and
write-ins to its "Total Votes Cast", Bergen's and Salem's printed percentages agree with
their votes, and Salem's precincts add up to its countywide table. Statewide totals are
the 21 counties' sums. The newest county update time is the report time.
"""

import hashlib
import html
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timedelta, timezone

from pypdf import PdfReader

from election_pipeline import elections
from election_pipeline.parsers import clarity
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, unique_json

WRITE_IN = ("write-in", "Write-in")
EASTERN = timezone(timedelta(hours=-5))
MAX_BYTES = 30_000_000
# The import zip's layout, for scripts/pack_import.py (election_pipeline.packing).
PACK = {"listing": "sources.json"}
MAX_PAGES = 400
# Leading or trailing party marks counties print beside a name.
MARKS = re.compile(
    r"^(?:REP|DEM|IND|NON|NP[A-D]|ETC|SWP|SIG|HSF|N-[A-F]|NM[0-9A-D]|NO[CD])\s+|\s+\([A-Z]\)$"
)
ESS = ("camden", "hunterdon", "sussex", "warren")
COUNT = r"[0-9]{1,3}(?:,[0-9]{3})*"


def table():
    raw = elections.table("nj-contests.json").decode("utf-8")
    return json.loads(raw)


def key(name):
    """A spelling's comparison key: party marks dropped, letters only, upper case."""
    return re.sub(r"[^A-Z]", "", MARKS.sub("", " ".join(str(name).split())).upper())


def number(text, label):
    if not re.fullmatch(COUNT, text):
        raise ValueError(f"New Jersey results: invalid {label} {text!r}")
    return count(int(text.replace(",", "")))


class Race:
    """One race's ballot candidates: {spelling key: (name, party)}."""

    def __init__(self, race, entry):
        self.race, self.names = race, {}
        for candidate in entry["candidates"]:
            for spelling in [candidate["name"], *candidate.get("spellings", [])]:
                self.names[key(spelling)] = (candidate["name"], candidate["party"])
        self.ballot = {name for name, _ in self.names.values()}

    def split(self, lines, label):
        """{name: votes} for the ballot candidates and the write-in total from a
        contest's (printed name, votes) lines, each ballot candidate found once."""
        found, write_in = {}, 0
        for printed, votes in lines:
            candidate = self.names.get(key(printed))
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


def clarity_county(bundle, county, titles, races, settings):
    """{race: lines} from a Clarity county's summary, and its update time."""
    raw = bundle.read(f"clarity/{county}.settings.json")
    record = unique_json(clarity.document(raw, settings))
    if str(record.get("parententityname")).lower() != "nj":
        raise ValueError(f"{county}: Clarity settings belong to another site")
    updated = clarity.text(record.get("websiteupdatedat"), "update time")
    summary = clarity.document(bundle.read(f"clarity/{county}.summary.json"), settings)
    found = {}
    for contest in unique_json(summary):
        race = titles.get(" ".join(str(contest["C"]).split()))
        if race is None:
            continue
        names, votes = contest["CH"], contest["V"]
        if race in found or contest["VF"] != 1 or len(names) != len(votes):
            raise ValueError(f"{county} {race}: malformed or repeated Clarity contest")
        lines = [(str(n), count(v)) for n, v in zip(names, votes)]
        if contest["T"] != [sum(v for _, v in lines)]:
            raise ValueError(f"{county} {race}: Clarity candidates disagree with the total")
        found[race] = lines
    return found, datetime.strptime(updated[:-4], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=EASTERN)


def pdf_text(raw, label, titles=None):
    """The PDF's pages as layout text. With ``titles``, pages are read only until every
    title has been seen and one more page after it (a summary report's precinct pages
    after its summary, 270 of Camden's 278, are not needed)."""
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError(f"{label}: PDF is encrypted or has an unexpected page count")
    # Camden's precinct pages print rotated headings, which pypdf warns of once a line.
    logger = logging.getLogger("pypdf")
    level = logger.level
    logger.setLevel(logging.ERROR)
    pages, seen = [], set()
    try:
        for page in reader.pages:
            pages.append(page.extract_text(extraction_mode="layout") or "")
            if titles is None:
                continue
            if seen == set(titles):
                break
            seen |= {
                t for t in titles if t in {" ".join(x.split()) for x in pages[-1].splitlines()}
            }
    finally:
        logger.setLevel(level)
    return pages


def ess_county(raw, county, titles):
    """{race: lines} from an ES&S Summary Results Report: each contest's candidate and
    write-in lines (TOTAL column). A contest ends with its "Write-In Totals" (and that
    line's "Not Assigned" detail) and, where the report prints it, "Total Votes Cast",
    which the lines must add up to; a report without it (Hunterdon) prints each line's
    vote methods, which must add up to its TOTAL instead."""
    pages = pdf_text(raw, county, titles)
    if "OFFICIAL" not in pages[0][:600] and "OFFICIAL" not in pages[min(1, len(pages) - 1)][:600]:
        raise ValueError(f"{county}: not the county's official summary report")
    lines = [line.strip() for page in pages for line in page.splitlines()]
    found = {}
    for title, race in titles.items():
        starts = [i for i, line in enumerate(lines) if " ".join(line.split()) == title]
        if not starts:
            raise ValueError(f"{county} {race}: contest {title!r} not in its report")
        # A report repeats each contest per precinct after its summary; the first is it.
        block, methods, total, written = [], [], None, False
        for line in lines[starts[0] + 1 :]:
            if line.startswith("Total Votes Cast"):
                total = number(re.search(COUNT, line[16:])[0], "total")
                break
            match = re.fullmatch(rf"(.+?)\s+({COUNT})((?:\s+(?:{COUNT}|[0-9.]+%))*)", line)
            if not match or line.startswith(("Vote For", "TOTAL", "Day", "Voting")):
                if written and line:
                    break
                continue
            label = match[1]
            if label == "Not Assigned":
                continue
            if written:
                break
            values = re.findall(COUNT + r"(?!\.|%|[0-9])", match[3])
            block.append((label, number(match[2], "votes")))
            methods.append([number(v, "votes") for v in values])
            written = label == "Write-In Totals"
        if not written:
            raise ValueError(f"{county} {race}: contest has no write-in line where expected")
        if total is not None:
            if sum(v for _, v in block) != total:
                raise ValueError(f"{county} {race}: lines do not add up to Total Votes Cast")
        elif not all(m and sum(m) == v for m, (_, v) in zip(methods, block)):
            raise ValueError(f"{county} {race}: a line's vote methods do not add up to it")
        found[race] = [(re.sub(r"^Write-In Totals$", "Write-in", n), v) for n, v in block]
    return found


def percent_checked(lines, label):
    """(name, votes) lines whose printed percentages agree with their votes (to 0.01%)."""
    total = sum(v for _, v, _ in lines)
    for name, votes, percent in lines:
        if total and abs(votes * 100 / total - percent) > 0.006:
            raise ValueError(f"{label}: {name}'s percentage disagrees with the votes")
    return [(n, v) for n, v, _ in lines]


def bergen_county(raw, titles):
    pages = pdf_text(raw, "bergen")
    text = "\n".join(pages)
    if "OFFICIAL 2026 GENERAL ELECTION RESULTS" not in text and "2026 General Election" not in text:
        raise ValueError("bergen: not the county's 2026 general election results")
    updated = re.search(r"Website Updated: ([0-9/]+ [0-9:]+ [AP]M)", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    found = {}
    for title, race in titles.items():
        start = lines.index(title) if title in lines else None
        if start is None:
            raise ValueError(f"bergen {race}: contest {title!r} not in its results")
        block = []
        for line in lines[start + 2 :]:
            match = re.fullmatch(rf"(.+?) ({COUNT}) ([0-9.]+)%", line)
            if not match:
                if block and line:
                    break
                continue
            block.append((match[1], number(match[2], "votes"), float(match[3])))
        found[race] = percent_checked(block, f"bergen {race}")
    when = datetime.strptime(updated[1], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=EASTERN)
    return found, when


SALEM_LINE = re.compile(r"\['((?:[^'\\]|\\.)*)',\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]")


def salem_county(raw, titles):
    """{race: lines} from Salem's results page: each countywide contest's data array
    ([name, mail, early, Election Day, provisional, total]), whose totals the contest's
    precinct tables must add up to. Its four methods do not add up to its totals (they
    leave out another kind of ballot), so only the totals are used."""
    page = raw.decode("utf-8", errors="replace")
    updated = re.search(r'id="lblLastPublishedDateTime" class="updated-at">([^<]+)<', page)
    found, precincts = {}, {}
    for match in re.finditer(r'<span class="race-title">([^<]+)</span>', page):
        title = html.unescape(" ".join(match[1].split()))
        place, _, contest = title.rpartition(" - ")
        if place and contest in titles:
            # A precinct's table: candidate name and total votes cells.
            body = page[match.end() : page.find("</tbody>", match.end())]
            cells = re.findall(
                r'<td dc="true"[^>]*><span[^>]*>([^<]+)</span>.*?<td dv="true"[^>]*>([^<]*)</td>',
                body,
                re.S,
            )
            sums = precincts.setdefault(titles[contest], {})
            for name, votes in cells:
                name = html.unescape(name.strip())
                sums[name] = sums.get(name, 0) + number(votes.strip(), "votes")
        elif not place and title in titles:
            race = titles[title]
            start = page.find("[[", match.end())
            data = page[start : page.find("]];", start) + 2]
            lines = []
            for name, *values in SALEM_LINE.findall(data):
                lines.append((html.unescape(name.replace("\\'", "'")), count(int(values[-1]))))
            if race in found or not lines:
                raise ValueError(f"salem {race}: countywide contest missing or repeated")
            found[race] = lines
    for race, lines in found.items():
        if precincts.get(race) != dict(lines):
            raise ValueError(f"salem {race}: precincts do not add up to the countywide table")
    when = datetime.strptime(updated[1].strip(), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=EASTERN)
    return found, when


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "NJ":
        raise ValueError("Unsupported New Jersey source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    reviewed = table()
    races = {race: Race(race, entry) for race, entry in reviewed["races"].items()}
    if set(races) != set(state_adapter("NJ").races):
        raise ValueError("New Jersey's reviewed table does not cover its races")
    counties = reviewed["counties"]
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if len(entries) > 60 or any(e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries):
            raise ValueError("New Jersey import has too many, too large or encrypted files")
        listing = json.loads(bundle.read("sources.json"))
        for name, entry in listing.items():
            if hashlib.sha256(bundle.read(name)).hexdigest() != entry["sha256"]:
                raise ValueError(f"New Jersey {name} differs from its listed SHA-256")
        if set(listing) != {e.filename for e in entries} - {"sources.json"}:
            raise ValueError("New Jersey import's listing does not match its files")
        results, times = {}, []
        for county, entry in sorted(counties.items()):
            titles = {" ".join(t.split()): race for t, race in entry["contests"].items()}
            kind = entry["format"]
            if kind == "clarity":
                results[county], when = clarity_county(bundle, county, titles, races, settings)
                times.append(when)
            elif kind == "ess":
                results[county] = ess_county(bundle.read(f"{county}.pdf"), county, titles)
            elif kind == "bergen":
                results[county], when = bergen_county(bundle.read("bergen.pdf"), titles)
                times.append(when)
            elif kind == "salem":
                results[county], when = salem_county(bundle.read("salem.html"), titles)
                times.append(when)
            else:
                raise ValueError(f"{county}: unknown New Jersey results format {kind!r}")
            if set(results[county]) != set(titles.values()):
                raise ValueError(f"{county}: a selected contest is missing")
    rows, statewide, places = [], {}, {}
    for county, by_race in sorted(results.items()):
        name = counties[county]["name"]
        for race, lines in by_race.items():
            found, write_in = races[race].split(lines, f"{name} County")
            totals = statewide.setdefault(race, {})
            for candidate, votes in found.items():
                party = next(p for n, p in races[race].names.values() if n == candidate)
                rows.append(row(race, race, candidate, candidate, party, votes, name))
                totals[candidate] = totals.get(candidate, 0) + votes
            rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_in, name))
            totals[WRITE_IN[1]] = totals.get(WRITE_IN[1], 0) + write_in
            places.setdefault(race, []).append(name)
    for race, totals in statewide.items():
        for candidate, votes in totals.items():
            if candidate == WRITE_IN[1]:
                rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", votes))
                continue
            party = next(p for n, p in races[race].names.values() if n == candidate)
            rows.append(row(race, race, candidate, candidate, party, votes))
        verify_scope("NJ", race, sorted(races[race].ballot), sorted(places[race]))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_generated_at=max(times).isoformat(),
        source_update_text=f"newest county update {max(times).isoformat()}",
        official_results=False,
        contest_total_reconciliation="passed",
    )
