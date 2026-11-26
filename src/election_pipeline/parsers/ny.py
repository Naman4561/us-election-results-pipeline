"""New York State Board of Elections: unofficial election night results, saved as a PDF.

The operator prints the State Board's results page, tracking Governor and the 26
congressional districts, to PDF and imports it as a manual source
(docs/manual-results.md). Each contest is printed for "All Counties" and then for each
county it covers ("Part of Kings" where a district takes part of a county), as a block:

    <Contest> > <Area> (Active Registered Voters: N)   Election Districts Reporting: a of b
    <Candidate>  <PARTY>  <%> <votes>  <% by candidate> <candidate votes>
                 <PARTY>  <%> <votes>          (the same candidate's other party lines)
    Blank / Void / Write-in  <%> <votes>  <%> <votes>
    Total Votes  N

The text is read in layout mode (plain extraction reorders some rows). New York
candidates can run on several party lines (fusion): each candidate's lines must add up
to the candidate's printed total; the candidates, blanks, voids and write-ins to the
area's Total Votes; and each contest's counties to its All Counties block. A candidate
is stored under its first (major) party line; blank and void ballots are not votes for
anyone and are not stored, and write-ins are one "Write-in" line.

The page prints its update time (Eastern).
"""

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

from pypdf import PdfReader

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.platforms import party_names

MAX_PAGES = 400
# Eastern Standard Time: the page's update time is from November, after daylight time.
EASTERN = timezone(timedelta(hours=-5))
GOVERNOR = "Governor and Lieutenant Governor"
HEADER = re.compile(
    r"\s*(Governor and Lieutenant Governor|\d+(?:st|nd|rd|th) Congressional District) > "
    r"(.+?) \(Active"
)
# The counts can wrap below the label, after the registered-voter count.
REPORTING = re.compile(r"Election Districts Reporting:.*?(?<![\d,])([\d,]+)\s+of\s+([\d,]+)")
PARTIES = party_names("NY")
CLOSING = ("Blank", "Void", "Write-in", "Total Votes")
# In the printed layout a candidate's name starts at the left margin; party labels start
# further right, in their own column.
NAME_COLUMN = 15
COUNT = r"\d{1,3}(?:,\d{3})*"
UPDATED = re.compile(r"(\d\d/\d\d/2026)\s+(\d{1,2}:\d\d:\d\d [AP]M)\s+Auto Refresh")
WRITE_IN = ("write-in", "Write-in")


def number(value):
    if not re.fullmatch(COUNT, value):
        raise ValueError(f"New York results: invalid count {value!r}")
    return int(value.replace(",", ""))


def race_name(contest):
    if contest == GOVERNOR:
        return "NY-GOV"
    district = int(re.match(r"\d+", contest)[0])
    return f"NY-{district:02}"


def segments(line):
    """(column, text) for each run of text separated by two or more spaces."""
    return [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]


def numeric(text):
    return re.fullmatch(r"[\d.,%\s]+", text) is not None


def block(lines, race, area):
    """[(name, party, votes)] and write-ins for one area, after checking every sum.

    A row with two vote counts (party votes and candidate votes, beside their percents)
    starts a candidate; a row with one adds another party line to it. Name fragments at
    the left margin, on that row or the rows after it, are the candidate's name. A party
    label can wrap above, beside and below its row ("Speak / The / Truth"): the row waits
    until its label completes one of the reviewed parties.
    """
    candidates, pending, closing, held = [], "", {}, None

    def apply(party, votes, name):
        if len(votes) == 2:
            candidates.append(
                {
                    "name": [],
                    "party": PARTIES[party],
                    "lines": number(votes[0]),
                    "total": number(votes[1]),
                }
            )
        elif len(votes) == 1 and candidates:
            candidates[-1]["lines"] += number(votes[0])
        else:
            raise ValueError(f"{race} {area}: unexpected New York result row")
        add_name(name)

    def add_name(name):
        if name:
            if not candidates:
                raise ValueError(f"{race} {area}: New York name before any result row")
            candidates[-1]["name"].extend(name)

    def prefix(label):
        return any(p.startswith(label + " ") for p in PARTIES)

    for line in lines:
        parts = segments(line)
        if not parts:
            continue
        stripped = line.strip()
        label = next((c for c in CLOSING if stripped.startswith(c)), None)
        if label:
            counts = re.findall(rf"(?<![\d.]){COUNT}(?![\d.])", stripped[len(label) :])
            if not counts or held:
                raise ValueError(f"{race} {area}: New York {label} row is out of place")
            closing[label] = number(counts[-1])
            continue
        if any("(by" in text or text in ("Party)", "Candidate)") for _, text in parts):
            continue
        name = [text for column, text in parts if column < NAME_COLUMN and not numeric(text)]
        rest = [text for column, text in parts if column >= NAME_COLUMN]
        words = " ".join(text for text in rest if not numeric(text))
        values = " ".join(text for text in rest if numeric(text))
        votes = re.findall(rf"(?<![\d.]){COUNT}(?![\d.])", values)
        if held:
            party, row_votes, row_name = held
            if votes or not words:
                raise ValueError(f"{race} {area}: unexpected New York party line {party!r}")
            party = f"{party} {words}"
            if party in PARTIES:
                held = None
                apply(party, row_votes, row_name)
                add_name(name)
            elif prefix(party):
                held = (party, row_votes, row_name + name)
            else:
                raise ValueError(f"{race} {area}: unexpected New York party line {party!r}")
            continue
        party = " ".join(x for x in (pending, words) if x)
        if votes:
            pending = ""
            if party in PARTIES:
                apply(party, votes, name)
            elif prefix(party):
                held = (party, votes, name)
            else:
                raise ValueError(f"{race} {area}: unexpected New York party line {party!r}")
            continue
        if words:
            pending = party
        add_name(name)
    if pending or held or set(closing) != set(CLOSING) or not candidates:
        raise ValueError(f"{race} {area}: New York block is incomplete")
    found = []
    for candidate in candidates:
        if candidate["lines"] != candidate["total"] or not candidate["name"]:
            raise ValueError(f"{race} {area}: party lines do not add up to the candidate total")
        # A name wrapped at a hyphen ("Ocasio-" / "Cortez") rejoins without a space.
        name = re.sub(r"- ", "-", " ".join(candidate["name"]))
        found.append((name, candidate["party"], candidate["total"]))
    total = sum(v for _, _, v in found)
    total += closing["Blank"] + closing["Void"] + closing["Write-in"]
    if total != closing["Total Votes"]:
        raise ValueError(f"{race} {area}: New York votes do not add up to Total Votes")
    return found, closing["Write-in"]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "NY":
        raise ValueError("Unsupported New York source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    if not raw.startswith(b"%PDF-"):
        raise ValueError("New York results are not a PDF")
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("New York results PDF is encrypted or has an unexpected page count")
    first = " ".join((reader.pages[0].extract_text() or "").split())
    if "New York State Unofficial Election Night Results" not in first[:400]:
        raise ValueError("New York results are not the State Board's election night page")
    layout = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages)
    if len(layout) > settings.max_uncompressed_bytes:
        raise ValueError("New York results text is too large")
    # The page's own header (its update time) reads cleanly in plain text.
    opening = " ".join(" ".join((p.extract_text() or "") for p in reader.pages[:2]).split())
    updated = UPDATED.search(opening)
    if not updated or "Election General Election" not in opening:
        raise ValueError("New York results lack their update time or general election")
    generated = datetime.strptime(" ".join(updated.groups()), "%m/%d/%Y %I:%M:%S %p")
    generated = generated.replace(tzinfo=EASTERN)
    wanted = state_adapter("NY").races
    lines = layout.splitlines()
    starts = [i for i, line in enumerate(lines) if HEADER.match(line)]
    statewide, counties, rows = {}, {}, []
    for index, start in enumerate(starts):
        header = HEADER.match(lines[start])
        race, area = race_name(header[1]), " ".join(header[2].split())
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        # The header wraps over a few lines; the column headings ("Graph (by ...") end it.
        heading = next((i for i in range(start, end) if "(by" in lines[i]), None)
        # The registered-voter count can wrap between "Reporting: 55" and "of 55".
        text = " ".join(" ".join(lines[start : heading or start]).split())
        status = heading and REPORTING.search(re.sub(r"Voters: [\d,]+\)", "", text))
        if race not in wanted or not status:
            raise ValueError(f"New York results: unexpected contest {header[1]!r}")
        reporting, total_units = number(status[1]), number(status[2])
        if reporting > total_units:
            raise ValueError(f"{race} {area}: more districts reporting than exist")
        body = lines[heading:end]
        last = next(
            (i for i, line in enumerate(body) if line.strip().startswith("Total Votes")), None
        )
        if last is None:
            raise ValueError(f"{race} {area}: New York block lacks Total Votes")
        found, write_ins = block(body[: last + 1], race, area)
        if area == "All Counties":
            if race in statewide:
                raise ValueError(f"{race}: New York statewide block repeats")
            statewide[race] = (found, write_ins)
            continue
        county = re.sub(r"^Part of ", "", area)
        if (race, county) in counties:
            raise ValueError(f"{race}: New York county {county} repeats")
        counties[(race, county)] = (found, write_ins)
        for name, party, votes in found:
            rows.append(row(race, race, name, name, party, votes, county, reporting, total_units))
        rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_ins, county,
                        reporting, total_units))  # fmt: skip
    if set(statewide) != set(wanted):
        raise ValueError("New York results lack a selected contest")
    for race, (found, write_ins) in statewide.items():
        for name, party, votes in found:
            rows.append(row(race, race, name, name, party, votes))
        rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_ins))
        places = sorted(county for r, county in counties if r == race)
        verify_scope("NY", race, [name for name, _, _ in found], places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=" ".join(updated.groups()),
        source_generated_at=generated.isoformat(),
        party_line_reconciliation="passed",
        contest_total_reconciliation="passed",
    )
