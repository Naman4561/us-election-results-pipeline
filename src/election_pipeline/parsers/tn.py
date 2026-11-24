"""Tennessee Secretary of State: the "Followed Races" dashboard, saved as a PDF by hand.

The operator prints the election night dashboard's Followed Race Results (Governor, U.S.
Senate, U.S. House 1-9) to PDF and imports it as a manual source
(docs/manual-results.md). It gives statewide totals only: each candidate as
"Name (P) - Votes" with its percentage of the race, and each race's "Final Unofficial
Results N of N Counties Completed". It prints no total votes, so every candidate's
printed percentage (two decimals) is checked against its votes over the race's sum,
which also proves each candidate sits under the right race in the printed text.

The page prints no report time; the operator declares the one the site showed.
"""

import io
import re
import zipfile

from pypdf import PdfReader

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row

MAX_PAGES = 50
SENATE = "United States Senate"
HOUSE = re.compile(r"United States House of Representatives District (\d)")
CANDIDATE = re.compile(r"(.+) \(([A-Z])\) - ([\d,]+)")
PERCENT = re.compile(r"(?:✔\s*)?(\d{1,3}\.\d\d)%(.*)")
COMPLETED = re.compile(r"Final Unof(?:fi|ﬁ)cial Results (\d+) of (\d+) Counties Completed")
PARTIES = {"R": "REP", "D": "DEM", "I": "IND", "L": "LIB", "G": "GRN"}


def number(value):
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*", value):
        raise ValueError(f"Tennessee results: invalid count {value!r}")
    return int(value.replace(",", ""))


def lines_of(raw, settings):
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Tennessee results PDF is encrypted or has an unexpected page count")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > settings.max_uncompressed_bytes:
        raise ValueError("Tennessee results text is too large")
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "TN":
        raise ValueError("Unsupported Tennessee source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Tennessee results are not a PDF")
    lines = lines_of(raw, settings)
    if lines[:1] != ["November 3, 2026 Election"] or "Followed Race Results" not in lines[:12]:
        raise ValueError("Tennessee results are not the November 3, 2026 followed races")
    completed = [COMPLETED.fullmatch(x) for x in lines]
    completed = [m for m in completed if m]
    if not completed or any(m[1] != m[2] for m in completed):
        raise ValueError("Tennessee results are not final for every county")
    wanted = state_adapter("TN").races
    # Candidates before the first U.S. heading are the Governor's, the page's first race.
    race, races = "TN-GOV", {"TN-GOV": []}
    pending = None
    queue = list(lines)
    while queue:
        line = queue.pop(0)
        heading = "TN-SEN" if line == SENATE else None
        if found := HOUSE.fullmatch(line):
            heading = f"TN-{int(found[1]):02}"
        if heading:
            if pending:
                raise ValueError(f"{race}: Tennessee candidate without its percentage")
            if heading != race:
                if heading in races:
                    raise ValueError(f"Tennessee results repeat {heading}")
                race = heading
                races[race] = []
            continue
        if found := CANDIDATE.fullmatch(line):
            if pending or found[2] not in PARTIES:
                raise ValueError(f"{race}: unexpected Tennessee candidate line {line!r}")
            pending = (found[1].strip(), PARTIES[found[2]], number(found[3]))
            continue
        if pending and (found := PERCENT.fullmatch(line)):
            races[race].append((*pending, found[1]))
            pending = None
            if found[2].strip():  # Text run on after the percentage is the next line.
                queue.insert(0, found[2].strip())
    if pending or set(races) != set(wanted) or not all(races.values()):
        raise ValueError("Tennessee results lack a selected race or a candidate")
    rows = []
    for race, found in races.items():
        total = sum(votes for _, _, votes, _ in found)
        names = [name for name, _, _, _ in found]
        if len(set(names)) != len(names):
            raise ValueError(f"{race}: duplicate Tennessee candidate")
        # The site's percentages are of a race total it does not print, which can include
        # votes for unlisted (write-in) candidates, and its rounding is not always to the
        # nearest hundredth (4,098 of 2,102,474, 0.1949%, prints 0.20%). One total must fit
        # every candidate's percentage within 0.01 and be at least the listed candidates' sum.
        low, high = total, float("inf")
        for name, _, votes, printed in found:
            share = float(printed)
            low = max(low, 100 * votes / (share + 0.01))
            if share > 0.01:
                high = min(high, 100 * votes / (share - 0.01))
        if low > high + 1e-6:
            raise ValueError(f"{race}: Tennessee votes disagree with the printed percentages")
        for name, party, votes, _ in found:
            rows.append(row(race, race, name, name, party, votes))
        verify_scope("TN", race, names)
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        counties_completed=f"{completed[0][1]} of {completed[0][2]}",
        percentage_reconciliation="passed",
    )
