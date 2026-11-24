"""Missouri Secretary of State: the "All Results" page, saved as a PDF by hand.

The results site refuses automated clients, so the operator prints its All Results page
to PDF in a browser and imports it as a manual source (docs/manual-results.md). The page
lists every contest's statewide totals, with no county breakdown: Missouri's 2026 races
here are the eight U.S. House districts. Each district lists its candidates
("Name Party Votes Percent"), its precincts reporting and "Total Votes: N", which the
candidates must add up to. Write-ins (the "WRITE-INS" line and any named write-in
candidate) are kept as one "Write-in" line per district.

The page prints its report time ("as of 11/18/2026 10:53:42 PM"), in Central time.
"""

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

from pypdf import PdfReader

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row

ELECTION = "State of Missouri - General Election, November 03, 2026"
# Central Standard Time: the report is from November, after daylight time ended.
CENTRAL = timezone(timedelta(hours=-6))
MAX_PAGES = 200
RACE = re.compile(
    r"U\.S\. Representative - District (\d)\s+([\d,]+) of ([\d,]+) Precincts Reported"
)
PARTIES = {
    "Republican": "REP",
    "Democratic": "DEM",
    "Libertarian": "LIB",
    "Green": "GRN",
    "Constitution": "CST",
}
CANDIDATE = re.compile(r"(.+?) (Republican|Democratic|Libertarian|Green|Constitution|Write-in)"
                       r" ([\d,]+) (\d+\.\d+)%")  # fmt: skip
TOTAL = re.compile(r"Total Votes: ([\d,]+)")
WRITE_IN = ("write-in", "Write-in")


def number(value):
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*", value):
        raise ValueError(f"Missouri results: invalid count {value!r}")
    return int(value.replace(",", ""))


def page_text(raw, settings):
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Missouri results PDF is encrypted or has an unexpected page count")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > settings.max_uncompressed_bytes:
        raise ValueError("Missouri results text is too large")
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "MO":
        raise ValueError("Unsupported Missouri source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Missouri results are not a PDF")
    lines = page_text(raw, settings)
    if ELECTION not in " ".join(lines[:12]):
        raise ValueError("Missouri results belong to another election")
    statuses = [x for x in lines[:12] if x in ("Unofficial Results", "Official Results")]
    status = statuses[0] if len(set(statuses)) == 1 else None
    if status is None:
        raise ValueError("Missouri results do not state whether they are official")
    stamp = next((m for m in (re.fullmatch(r"as of (.+)", x) for x in lines[:12]) if m), None)
    if stamp is None:
        raise ValueError("Missouri results lack their report time")
    generated = datetime.strptime(stamp[1], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=CENTRAL)
    wanted = state_adapter("MO").races
    rows, seen, candidates = [], set(), {}
    index = 0
    while index < len(lines):
        match = RACE.fullmatch(lines[index])
        index += 1
        if not match:
            continue
        race = f"MO-{int(match[1]):02}"
        if race not in wanted or race in seen:
            raise ValueError(f"Missouri results: unexpected or repeated {race}")
        seen.add(race)
        reporting, total_units = number(match[2]), number(match[3])
        if reporting > total_units:
            raise ValueError(f"{race}: more precincts reporting than exist")
        names, write_ins, write_in_lines, total = {}, 0, 0, None
        while index < len(lines):
            line = lines[index]
            index += 1
            if found := TOTAL.fullmatch(line):
                total = number(found[1])
                break
            found = CANDIDATE.fullmatch(line)
            if not found:
                raise ValueError(f"{race}: unexpected Missouri result line {line!r}")
            name, affiliation, votes = found[1].strip(), found[2], number(found[3])
            if affiliation == "Write-in":
                write_ins += votes
                write_in_lines += 1
                continue
            if name in names:
                raise ValueError(f"{race}: duplicate Missouri candidate {name!r}")
            names[name] = (PARTIES[affiliation], votes)
        if total is None or sum(v for _, v in names.values()) + write_ins != total:
            raise ValueError(f"{race}: Missouri candidates do not add up to Total Votes")
        for name, (party, votes) in names.items():
            rows.append(row(race, race, name, name, party, votes, reporting=reporting,
                            total=total_units))  # fmt: skip
        if write_in_lines:
            rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_ins,
                            reporting=reporting, total=total_units))  # fmt: skip
        candidates[race] = list(names)
    if seen != set(wanted):
        raise ValueError("Missouri results lack a U.S. House district")
    for race in sorted(wanted):
        verify_scope("MO", race, candidates[race])
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        source_update_text=stamp[0],
        source_generated_at=generated.isoformat(),
        official_results=status == "Official Results",
        contest_total_reconciliation="passed",
    )
