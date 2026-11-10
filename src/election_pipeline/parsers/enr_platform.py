"""Strict readers for the ASP.NET election-night platform used by SD, NM (and NE).

The statewide CSV, the results pages and the county workbooks are all official output
of one state site. Nothing here infers a missing value: unknown means rejection, or,
only where a sheet explicitly prints ``*``, "not reported".
"""

import csv
import io
import re

from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.structured import count

POSTBACK = re.compile(r"__doPostBack\('(ctl00\$MainContent\$[A-Za-z0-9_$]+)',''\)")


def read_csv(raw, header, label):
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError(f"{label} CSV contains NUL bytes")
    reader = csv.reader(io.StringIO(text), strict=True)
    if next(reader, []) != header:
        raise ValueError(f"Unexpected {label} CSV header")
    for line, values in enumerate(reader, 2):
        if len(values) == len(header) + 1 and values[-1] == "":
            values.pop()
        if len(values) != len(header) or line > 100_000:
            raise ValueError(f"Unexpected {label} field count/row limit")
        values = dict(zip(header, (v.strip() for v in values)))
        for field in ("RaceID", "CandidateID", "CandidateVotes"):
            if not re.fullmatch(r"[0-9]+", values[field]):
                raise ValueError(f"{label} {field} must be a nonnegative integer")
        yield line, values


def text(node):
    return " ".join(node.text.split())


def page_text(page):
    return " ".join(Tree(page).root.text.split())


def statewide_update(page, state, election):
    """Check a results page's election identity and return its 'last updated' text.

    None when the page shows no time: South Dakota emptied its "last updated" box on
    Nov 10, 2026. The page still has to name the state and the election, and two
    different times are refused.
    """
    body = page_text(page)
    if f"{state} Secretary of State" not in body or election not in body:
        raise ValueError(f"{state} results page belongs to another election")
    found = re.findall(
        r"Results last updated: ([0-9]{1,2}/[0-9]{1,2}/2026 [0-9]{1,2}:[0-9]{2} [AP]M [A-Z]{2})",
        body,
    )
    if len(set(found)) > 1:
        raise ValueError(f"{state} results page shows more than one update time")
    return found[0] if found else None


def export_target(page, *, link_text=None, button_value=None, race_id=None):
    """The event target of exactly one export link or button on an official page."""
    root = Tree(page).root
    if link_text is not None:
        nodes = [n for n in root.find("a") if text(n) == link_text]
        scripts = [n.attrs.get("href", "") for n in nodes]
    else:
        nodes = [
            n
            for n in root.find("input")
            if n.attrs.get("value") == button_value and n.attrs.get("raceid") == race_id
        ]
        scripts = [n.attrs.get("onclick", "") for n in nodes]
    if len(nodes) != 1:
        raise ValueError("Expected exactly one official export control")
    calls = POSTBACK.findall(scripts[0])
    match = [None, calls[0]] if len(calls) == 1 else None
    if not match or (link_text is None and nodes[0].attrs.get("name") != match[1]):
        raise ValueError("Export control does not post back to its own page")
    return match[1]


def name_key(value):
    return " ".join(str(value).replace('"', "'").split())


def number(value, *, unreported=False):
    if type(value) is int:
        return count(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return count(int(value.strip()))
    if unreported and isinstance(value, str) and value.strip() == "*":
        return None
    raise ValueError(f"Invalid county workbook count: {value!r}")


def sheet_rows(book, sheet, race_name, *, title, state_line, unreported=False):
    """Read one official county sheet: ({candidate: column}, {county: [values]}, totals)."""
    rows = book.rows(sheet)
    if not rows or not rows[0] or rows[0][0] not in title:
        raise ValueError("County workbook belongs to another election")
    if len(rows) < 2 or rows[1] != [state_line]:
        raise ValueError("County workbook belongs to another state")
    headers = [i for i, r in enumerate(rows) if len(r) > 2 and r[1] == "County"]
    if len(headers) != 1 or name_key(rows[headers[0]][0]) != race_name:
        raise ValueError(f"{race_name}: unexpected county workbook header")
    header = rows[headers[0]]
    candidates = [name_key(c) for c in header[2:]]
    if not candidates or len(set(candidates)) != len(candidates) or "" in candidates:
        raise ValueError(f"{race_name}: invalid county workbook candidates")
    counties, totals, finished = {}, None, False
    for values in rows[headers[0] + 1 :]:
        if not any(v not in (None, "") for v in values):
            continue
        if finished or len(values) != len(header) or values[0] not in (None, ""):
            raise ValueError(f"{race_name}: unexpected county workbook row")
        label = " ".join(str(values[1] or "").split())
        cells = [number(v, unreported=unreported) for v in values[2:]]
        if label == "TOTALS":
            totals, finished = cells, True
            continue
        county = re.sub(r" \(Vote Center\)$", "", label).upper()
        if not county or county in counties:
            raise ValueError(f"{race_name}: missing or duplicate county row")
        counties[county] = cells
    if totals is None or not counties:
        raise ValueError(f"{race_name}: county workbook lacks its TOTALS row")
    return candidates, counties, totals


def county_sheet(book, sheet, race_name, official, *, title, state_line, masked=False):
    """County votes by candidate, checked against ``official`` statewide totals.

    Returns ``(counties, hidden)``. Without ``masked`` every cell must be a number and
    both TOTALS and the county sum must equal ``official``. With ``masked`` a ``*`` cell is
    a count the state hid for voter privacy. Such a county is left out of that race
    entirely (``hidden``) rather than shown with a candidate missing. TOTALS must still
    equal the sum of the printed cells, and must equal ``official`` for every candidate
    without a hidden cell; a candidate with one may only fall short of it.
    """
    candidates, counties, totals = sheet_rows(
        book, sheet, race_name, title=title, state_line=state_line, unreported=masked
    )
    keys = {name_key(name): name for name in official}
    if len(keys) != len(official) or set(candidates) != set(keys):
        raise ValueError(f"{race_name}: county workbook candidates differ from the statewide CSV")
    names = [keys[c] for c in candidates]
    if None in totals:
        raise ValueError(f"{race_name}: county workbook TOTALS are not numeric")
    sums = [sum(cells[i] or 0 for cells in counties.values()) for i in range(len(names))]
    if sums != totals:
        raise ValueError(f"{race_name}: county rows do not add up to the workbook TOTALS")
    for i, name in enumerate(names):
        if any(cells[i] is None for cells in counties.values()):
            if totals[i] > official[name]:
                raise ValueError(f"{race_name}: printed county votes exceed the statewide CSV")
        elif totals[i] != official[name]:
            raise ValueError(f"{race_name}: county workbook TOTALS differ from the statewide CSV")
    hidden = sorted(county for county, cells in counties.items() if None in cells)
    shown = {
        county: dict(zip(names, cells))
        for county, cells in counties.items()
        if county not in hidden
    }
    return shown, hidden


def find_sheet(book, prefix, race_name):
    """The one sheet named ``prefix`` + race name, which the site truncates to 30 chars."""
    full = prefix + race_name
    matches = [s for s in book.sheets if s.startswith(prefix) and full.startswith(s.strip())]
    if prefix.isdigit():
        matches = [s for s in matches if not s[len(prefix) : len(prefix) + 1].isdigit()]
    if len(matches) != 1 or len(matches[0]) < min(len(full), 30):
        raise ValueError(f"{race_name}: expected one '{prefix}' county sheet")
    return matches[0]
