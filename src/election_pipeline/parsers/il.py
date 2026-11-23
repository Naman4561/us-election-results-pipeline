"""Illinois: each election authority's own results, assembled into one zip as they come in.

The State Board of Elections posts results only after its canvass, in December; each of
Illinois's 108 election authorities posts its own before then: the county clerks or
election commission of the 102 counties, and six city election commissions (Bloomington,
Chicago, Danville, East St. Louis, Galesburg, Rockford), each inside one county. The
import, ``results.zip``, holds ``manifest.json`` and each collected authority's files
unchanged. The manifest names, for each authority, its files and their SHA-256s, where
they were posted, when they were saved and the results' own time, whether they are the
authority's canvass (``canvassed``) or election-night count (``unofficial``), and how
they are read:

- ``clarity``: the authority's Clarity results site (``summary.json`` and
  ``electionsettings.json`` of one published version). Each contest's candidates must
  add up to its total; its precincts reporting are kept, so a count still under way
  (McLean's, in November 2026) is shown as partial.
- ``chicago``: the Chicago Board of Elections' results, one file per race: what its
  results page's form returns for the race citywide (each file's ``role`` is the race it
  was requested for). The candidates must add up to the citywide total, and the 50
  wards' totals to each candidate's citywide count. The responses do not name their
  contest, so each race's candidates must be its reviewed ballot.
- ``platinum``: a county's platinumelectionresults.com race pages (Federal, State and
  Congressional District) for the November 3 election. Each candidate's vote methods
  (Election Day, Vote By Mail, Early Voting, Provisional, Other) must add up to its
  total, which is its count; each contest's precincts reporting are kept.
- ``review``: results no reader takes. Their counts are transcribed into
  ``<Authority>.review.json`` for the files' SHA-256s and signed by the operator who
  checked them; where the results print a contest total, the counts must add up to it.

The reviewed table ``resources/il_2026_contests.json`` names every race's ballot
candidates, their parties and the spellings authorities print, and each authority's own
title for each selected contest. Each authority must report only the races of its
county, and a county is published once every authority in it is collected: its counts
are its authorities' sums, and between them they must report every race the roster
places the county in. In each race every ballot candidate is found once, and every other
line must be a write-in line; write-ins are kept as one "Write-in" line (a name not in
the table is refused, so a new spelling is reviewed, not counted as a write-in).
Statewide votes are the published
counties' sums (``State.collected_counties``); the newest results time is the import's.
"""

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from html import unescape
from importlib.resources import files

from election_pipeline.parsers import clarity
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.parsers.structured import (
    count,
    formatted_count,
    row,
    text,
    unique_json,
)

WRITE_IN = ("write-in", "Write-in")
MAX_BYTES = 30_000_000
WRITTEN = re.compile(r"(?i)write[- ]?in")
REPORTS = ("canvassed", "unofficial")
FORMATS = ("clarity", "chicago", "platinum", "review")
# The six city election commissions and the county each lies in; every other authority
# is its county's own and is named by it.
CITIES = {
    "Bloomington": "McLean",
    "Chicago": "Cook",
    "Danville": "Vermilion",
    "East St. Louis": "St. Clair",
    "Galesburg": "Knox",
    "Rockford": "Winnebago",
}
# Clarity prints its update time in Central time (Standard or Daylight).
CENTRAL = {"CST": timezone(timedelta(hours=-6)), "CDT": timezone(timedelta(hours=-5))}
CLARITY_PARENTS = {"il", "wrc"}


def table():
    raw = files("election_pipeline").joinpath("resources/il_2026_contests.json").read_text("utf-8")
    return json.loads(raw)


def key(name):
    """A spelling's comparison key: letters only, upper case."""
    return re.sub(r"[^A-Z]", "", str(name).upper())


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
                if not WRITTEN.search(printed):
                    raise ValueError(f"{self.race} {label}: unknown candidate {printed!r}")
                write_in += votes
                continue
            if candidate[0] in found:
                raise ValueError(f"{self.race} {label}: {candidate[0]} printed twice")
            found[candidate[0]] = votes
        if set(found) != self.ballot:
            missing = sorted(self.ballot - set(found))
            raise ValueError(f"{self.race} {label}: ballot candidate missing: {missing}")
        return found, write_in


def clarity_results(settings_raw, summary_raw, site, titles, settings):
    """{race: (lines, (reporting, total))} from a Clarity site's summary, and its update time."""
    record = unique_json(clarity.document(settings_raw, settings))
    if record.get("entityname") != site or record.get("parententityname") not in CLARITY_PARENTS:
        raise ValueError(f"{site}: Clarity settings belong to another site")
    updated = text(record.get("websiteupdatedat"), "update time")
    match = re.fullmatch(r"(.+) (C[SD]T)", updated)
    if not match:
        raise ValueError(f"{site}: unexpected Clarity update time {updated!r}")
    when = datetime.strptime(match[1], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=CENTRAL[match[2]])
    found = {}
    for contest in unique_json(clarity.document(summary_raw, settings)):
        race = titles.get(" ".join(str(contest["C"]).split()))
        if race is None:
            continue
        names, votes = contest["CH"], contest["V"]
        if race in found or contest["VF"] != 1 or not names or len(names) != len(votes):
            raise ValueError(f"{site} {race}: malformed or repeated Clarity contest")
        lines = [(text(n, "candidate"), count(v)) for n, v in zip(names, votes)]
        if contest["T"] != [sum(v for _, v in lines)]:
            raise ValueError(f"{site} {race}: Clarity candidates disagree with the total")
        reporting, total = count(contest["PR"]), count(contest["TP"])
        if reporting > total or total == 0:
            raise ValueError(f"{site} {race}: unexpected precincts reporting")
        found[race] = (lines, (reporting, total))
    return found, when


def cells(markup):
    """Each table's rows of cell texts."""
    tables = []
    for table in re.findall(r"<table.*?</table>", markup, re.S):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", table, re.S):
            found = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)
            rows.append([" ".join(unescape(re.sub(r"<[^>]+>", " ", c)).split()) for c in found])
        tables.append(rows)
    return tables


def chicago_results(raw, race):
    """A race's lines from the Chicago Board of Elections' results for it: the response
    its results page's form returns (Drupal AJAX commands whose dialog holds a citywide
    table, then one table per ward). The candidates must add up to the citywide total
    votes, and each candidate's ward totals to its citywide count."""
    commands = unique_json(raw)
    if not isinstance(commands, list):
        raise ValueError(f"Chicago {race}: unexpected results response")
    markup = "".join(
        c["data"] for c in commands if isinstance(c, dict) and isinstance(c.get("data"), str)
    )
    tables = cells(markup)
    if not tables or len(tables[0]) != 2 or tables[0][0][:1] != ["Total Votes"]:
        raise ValueError(f"Chicago {race}: results lack their citywide table")
    head, city = tables[0]
    names = head[1::2]
    if len(head) != 1 + 2 * len(names) or len(city) != len(head) or not names:
        raise ValueError(f"Chicago {race}: malformed citywide table")
    total = formatted_count(city[0])
    votes = [formatted_count(v) for v in city[1::2]]
    if sum(votes) != total:
        raise ValueError(f"Chicago {race}: candidates do not add up to the total votes")
    wards = tables[1:]
    sums = [0] * len(names)
    for ward in wards:
        if not ward or ward[0][2::2] != names or ward[-1][:1] != ["Total"]:
            raise ValueError(f"Chicago {race}: a ward table lists other candidates")
        for index, value in enumerate(ward[-1][2::2]):
            sums[index] += formatted_count(value)
    if not wards or sums != votes:
        raise ValueError(f"Chicago {race}: wards do not add up to the citywide counts")
    return list(zip(names, votes, strict=True))


PLATINUM_METHODS = ["Election Day", "Vote By Mail", "Early Voting", "Provisional", "Other", "Total"]


def platinum_results(pages, titles, report, label):
    """{race: (lines, precincts)} from a county's platinumelectionresults race pages (its
    Federal, State and Congressional District pages). Each candidate's vote methods must
    add up to its total, which is its count; a page marked unofficial cannot be the
    county's canvass."""
    found = {}
    for raw in pages:
        page = raw.decode("utf-8")
        heading = re.search(r'<h2 id="electionTitle">(.*?)</h2>', page, re.S)
        heading = " ".join(unescape(heading[1]).split()) if heading else ""
        match = re.fullmatch(
            r"GENERAL ELECTION \(11/03/2026\) (Unofficial|Official) Results", heading
        )
        if not match:
            raise ValueError(f"{label}: race page belongs to another election ({heading!r})")
        if match[1] == "Unofficial" and report == "canvassed":
            raise ValueError(f"{label}: an unofficial page is not the county's canvass")
        for card in page.split('<div class="card h-100">')[1:]:
            title = re.search(r"<h3>(.*?)</h3>", card, re.S)
            title = " ".join(unescape(re.sub(r"<[^>]+>", " ", title[1])).split()) if title else ""
            race = titles.get(title)
            if race is None:
                continue
            done = re.search(r"([0-9]+) of ([0-9]+) precincts fully reporting", card)
            if race in found or not done:
                raise ValueError(f"{label} {race}: repeated contest or no precincts reporting")
            reporting, total = int(done[1]), int(done[2])
            if reporting > total or total == 0:
                raise ValueError(f"{label} {race}: unexpected precincts reporting")
            lines = []
            candidates = re.findall(
                r'can-line flex-grow-1">(.*?)</div>\s*'
                r'<div class="p-2[^"]*can-line">\s*([0-9,]+)\s*</div>'
                r"(.*?)</table>",
                card,
                re.S,
            )
            for name, votes, detail in candidates:
                heads = [
                    " ".join(h.split()) for h in re.findall(r"<th\b[^>]*>(.*?)</th>", detail, re.S)
                ]
                cells = [
                    formatted_count(c.strip()) for c in re.findall(r"<td>(.*?)</td>", detail, re.S)
                ]
                votes = formatted_count(votes)
                if heads != PLATINUM_METHODS or len(cells) != len(heads):
                    raise ValueError(f"{label} {race}: unexpected vote-method table")
                if sum(cells[:-1]) != cells[-1] or cells[-1] != votes:
                    raise ValueError(f"{label} {race}: vote methods do not add up to the count")
                # A ticket's names are printed on two lines (a <br/> between them).
                name = " ".join(unescape(re.sub(r"<[^>]+>", " ", name)).split())
                lines.append((text(name, "candidate"), votes))
            if not lines:
                raise ValueError(f"{label} {race}: no candidates read")
            found[race] = (lines, (reporting, total))
    return found


def review(document, sha256s, label):
    """{race: (lines, precincts or None)} from a signed review of an authority's counts."""
    listed = document.get("file_sha256")
    listed = [listed] if isinstance(listed, str) else listed
    if listed != sha256s or not str(document.get("reviewed_by", "")).strip():
        raise ValueError(f"{label}: review is unsigned or for another file")
    found = {}
    for race, counts in document["races"].items():
        lines = [(name, count(votes)) for name, votes in counts.items()]
        printed = (document.get("totals") or {}).get(race)
        if printed is not None and sum(v for _, v in lines) != printed:
            raise ValueError(f"{label} {race}: reviewed counts do not add up to the report")
        precincts = (document.get("precincts") or {}).get(race)
        if precincts is not None:
            reporting, total = (count(n) for n in precincts)
            if reporting > total or total == 0:
                raise ValueError(f"{label} {race}: unexpected precincts reporting")
            precincts = (reporting, total)
        found[race] = (lines, precincts)
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "IL":
        raise ValueError("Unsupported Illinois source/election")
    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(source.primary_filename)
    reviewed = table()
    adapter = state_adapter("IL")
    if set(reviewed["races"]) != set(adapter.races):
        raise ValueError("Illinois's reviewed table does not cover its races")
    # A race whose ballot is not reviewed yet takes no authority.
    races = {race: Race(race, e) for race, e in reviewed["races"].items() if e["candidates"]}
    roster = ROSTERS["IL"]
    counties = set(roster["IL-SEN"]["counties"])
    if not set(CITIES.values()) <= counties:
        raise ValueError("Illinois city commissions lie outside the roster's counties")
    county_of = {**{c: c for c in counties}, **CITIES}
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        entries = bundle.infolist()
        if len(entries) > 400 or any(e.file_size > MAX_BYTES or e.flag_bits & 1 for e in entries):
            raise ValueError("Illinois import has too many, too large or encrypted files")
        manifest = json.loads(bundle.read("manifest.json"))["authorities"]
        expected = {"manifest.json"}
        results, times, kinds = {}, [], {}
        for authority, entry in sorted(manifest.items()):
            label = authority
            if authority not in county_of:
                raise ValueError(f"Illinois has no election authority {authority!r}")
            if entry["report"] not in REPORTS or entry["format"] not in FORMATS:
                raise ValueError(f"{label}: unknown report kind or format")
            parts = entry["files"]
            data = {}
            for part in parts:
                body = bundle.read(part["file"])
                if hashlib.sha256(body).hexdigest() != part["sha256"]:
                    raise ValueError(f"{label}: file differs from its listed SHA-256")
                data[part.get("role", part["file"])] = body
                expected.add(part["file"])
            saved = datetime.fromisoformat(entry["saved_at"])
            if saved.tzinfo is None:
                raise ValueError(f"{label}: saving time is missing a timezone")
            if entry["format"] == "clarity":
                if set(data) != {"settings", "summary"}:
                    raise ValueError(f"{label}: a Clarity site is its settings and summary")
                titles = reviewed["titles"].get(authority) or {}
                found, reported = clarity_results(
                    data["settings"], data["summary"], entry["site"], titles, settings
                )
            elif entry["format"] == "platinum":
                titles = reviewed["titles"].get(authority) or {}
                found = platinum_results(list(data.values()), titles, entry["report"], label)
                reported = entry["report_time"] and datetime.fromisoformat(entry["report_time"])
            elif entry["format"] == "chicago":
                if authority != "Chicago" or not set(data) <= set(adapter.races):
                    raise ValueError(f"{label}: Chicago's results are one file per race")
                found = {race: (chicago_results(body, race), None) for race, body in data.items()}
                reported = entry["report_time"] and datetime.fromisoformat(entry["report_time"])
            else:
                name = f"{authority}.review.json"
                expected.add(name)
                document = json.loads(bundle.read(name))
                if document.get("authority") != authority:
                    raise ValueError(f"{label}: review names another authority")
                found = review(document, [part["sha256"] for part in parts], label)
                reported = entry["report_time"] and datetime.fromisoformat(entry["report_time"])
            if reported and (reported.tzinfo is None or reported > saved):
                raise ValueError(f"{label}: results time is missing a timezone or follows saving")
            county = county_of[authority]
            wanted = {race for race in adapter.races if county in roster[race]["counties"]}
            if not set(found) <= wanted or not found:
                raise ValueError(f"{label}: reports {sorted(found)}, outside {county}'s races")
            if set(found) - set(races):
                raise ValueError(f"{label}: {sorted(set(found) - set(races))} not yet reviewed")
            results[authority], kinds[authority] = found, entry["report"]
            times += [reported] if reported else []
        if {e.filename for e in entries} != expected:
            raise ValueError("Illinois import's files do not match its manifest")
    if not results or not times:
        raise ValueError("Illinois import has no authority, or no results time")
    # A county is published once every authority in it is collected.
    members = {}
    for authority, county in county_of.items():
        members.setdefault(county, []).append(authority)
    complete = sorted(c for c in counties if all(a in results for a in members[c]))
    waiting = sorted(
        c for c in counties if c not in complete and any(a in results for a in members[c])
    )
    rows, statewide, places, partial = [], {}, {}, set()
    for county in complete:
        wanted = {race for race in adapter.races if county in roster[race]["counties"]}
        reported = set().union(*(results[a] for a in members[county]))
        if reported != wanted:
            raise ValueError(
                f"{county}: reports {sorted(reported)}, but its races are {sorted(wanted)}"
            )
        for race in sorted(wanted):
            votes, write_in, precincts = {}, 0, (0, 0)
            for authority in members[county]:
                if race not in results[authority]:
                    continue
                lines, counted = results[authority][race]
                found, extra = races[race].split(lines, authority)
                for candidate, n in found.items():
                    votes[candidate] = votes.get(candidate, 0) + n
                write_in += extra
                precincts = (
                    None
                    if precincts is None or counted is None
                    else (precincts[0] + counted[0], precincts[1] + counted[1])
                )
            reporting, total = precincts if precincts else (None, None)
            if precincts and reporting < total:
                partial.add(county)
            totals = statewide.setdefault(race, {})
            for candidate, n in sorted(votes.items()):
                party = races[race].party[candidate]
                rows.append(
                    row(race, race, candidate, candidate, party, n, county, reporting, total)
                )
                totals[candidate] = totals.get(candidate, 0) + n
            rows.append(
                row(race, race, WRITE_IN[0], WRITE_IN[1], "", write_in, county, reporting, total)
            )
            totals[WRITE_IN[1]] = totals.get(WRITE_IN[1], 0) + write_in
            places.setdefault(race, []).append(county)
    if not complete:
        raise ValueError("Illinois import completes no county")
    for race, totals in sorted(statewide.items()):
        for candidate, votes in totals.items():
            if candidate == WRITE_IN[1]:
                rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", votes))
                continue
            rows.append(row(race, race, candidate, candidate, races[race].party[candidate], votes))
        verify_scope("IL", race, sorted(races[race].ballot), sorted(places[race]), collected=True)
    finish(
        rows, source, settings, output, stats, reference=reference, reconcile=True,
        races=sorted(statewide),
    )  # fmt: skip
    canvassed = sum(kinds[a] == "canvassed" for a in results)
    newest = max(times)
    stats.update(
        source_generated_at=newest.isoformat(),
        source_update_text=(
            f"{len(complete)} of 102 counties ({len(results)} of 108 election authorities, "
            f"{canvassed} canvassed); partial counts: {', '.join(sorted(partial)) or 'none'}; "
            f"newest results {newest.isoformat()}"
        ),
        official_results=False,
        contest_total_reconciliation="passed",
        counties_waiting=waiting,
        authority_reports={
            a: {k: manifest[a].get(k) for k in ("report", "format", "url", "report_time")}
            for a in sorted(results)
        },
    )
