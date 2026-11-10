"""South Carolina Election Commission results, with method and county reconciliation.

The source is the commission's Clarity site. Its ``detail.zip`` ("Detail XML") splits
each choice's votes by county and six vote methods; the methods must add up to each
county's count, the counties to the statewide count, and that to the official summary.
Only totals are stored. Snapshots taken before the switch hold the commission's
election-night workbook and its fallback index page instead, and still parse.
"""

import re
import zipfile

from election_pipeline.collectors.sc import latest_report
from election_pipeline.parsers import clarity
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, party, row, text, xml
from election_pipeline.parsers.workbook import Workbook
from election_pipeline.states import state_adapter

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}
METHODS = (
    "Election Day",
    "Early Voting",
    "Absentee By Mail",
    "Failsafe",
    "Provisional",
    "Failsafe Provisional",
    "Total Votes",
)


def race_name(name):
    name = text(name)
    if name in {"U.S. Senate (Vote For 1)", "Governor and Lieutenant Governor (Vote For 1)"}:
        return "SC-SEN" if name.startswith("U.S.") else "SC-GOV"
    match = re.fullmatch(r"U\.S\. House of Representatives, District (\d+) \(Vote For 1\)", name)
    return f"SC-{int(match[1]):02}" if match else None


def clarity_race(name):
    # The site doubles some spaces ("U.S.  Senate", "District  1").
    name = " ".join(name.split())
    if name in {"U.S. Senate", "Governor and Lieutenant Governor"}:
        return "SC-SEN" if name.startswith("U.S.") else "SC-GOV"
    match = re.fullmatch(r"U\.S\. House of Representatives, District ([1-7])", name)
    return f"SC-0{match[1]}" if match else None


def label_party(label):
    """A choice label is its party code and name ("REP Alan Wilson"); write-ins have none."""
    if label == "Write-In":
        return label, ""
    match = re.fullmatch(r"([A-Z]{3}) (.+)", label)
    if not match:
        raise ValueError("Unexpected SC candidate label")
    return match[2], party(match[1])


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported South Carolina source/election")
    with zipfile.ZipFile(archive_path) as archive:
        workbook = "results.xlsx" in archive.namelist()
    if workbook:
        parse_workbook(archive_path, source, settings, output, stats, reference)
    else:
        parse_clarity(archive_path, source, settings, output, stats, reference)


def parse_clarity(archive_path, source, settings, output, stats, reference):
    wanted = state_adapter("SC").races
    with zipfile.ZipFile(archive_path) as archive:
        updated = clarity.settings_check(archive.read("settings.json"), "SC", settings)
        final = clarity.final_upload(archive.read("settings.json"), settings)
        official = clarity.summary(archive.read("summary.json"), clarity_race, settings)
        root = xml(clarity.detail_member(archive.read("detail.zip"), settings, "xml"))
    found = clarity.detail_contests(
        root, wanted, clarity_race, "SC", "South Carolina", "2026 Statewide General Election"
    )
    if set(official) != set(wanted):
        raise ValueError("South Carolina summary lacks a selected contest")
    rows = []
    for race, contest in found.items():
        key, expected = official[race]
        if contest.get("key") != key:
            raise ValueError(f"{race}: detail and summary contest keys differ")
        precincts = {}
        for county in contest.findall("ParticipatingCounties/County"):
            name = text(county.get("name"), "county").upper()
            done = clarity.number(county.get("precinctsReported"), "precinct count", "SC")
            total = clarity.number(county.get("precinctsParticipating"), "precinct count", "SC")
            if name in precincts or done > total:
                raise ValueError(f"{race}: invalid county reporting status")
            precincts[name] = (done, total)
        if race in {"SC-SEN", "SC-GOV"} and len(precincts) != 46:
            raise ValueError(f"{race}: missing SC county coverage")
        choices = contest.findall("Choice")
        if len(choices) != len(expected):
            raise ValueError(f"{race}: detail and summary candidates differ")
        labels = []
        for choice, (listed, listed_party, votes) in zip(choices, expected, strict=True):
            label = text(choice.get("text"), "candidate")
            name, affiliation = label_party(label)
            if (
                label != listed
                or (choice.get("party") or "").strip() != listed_party
                or clarity.number(choice.get("totalVotes"), "vote count", "SC") != votes
            ):
                raise ValueError(f"{race}: detail and summary candidate totals differ")
            types = choice.findall("VoteType")
            if [t.get("name") for t in types] != list(METHODS[:6]):
                raise ValueError(f"{race}: unexpected SC vote methods")
            by_county = dict.fromkeys(precincts, 0)
            for kind in types:
                seen = set()
                method_total = 0
                for county in kind.findall("County"):
                    place = text(county.get("name"), "county").upper()
                    if place in seen or place not in precincts:
                        raise ValueError(f"{race}: unexpected or duplicate county {place}")
                    seen.add(place)
                    votes_here = clarity.number(county.get("votes"), "vote count", "SC")
                    by_county[place] += votes_here
                    method_total += votes_here
                if seen != set(precincts):
                    raise ValueError(f"{race}: a vote method lacks a participating county")
                if clarity.number(kind.get("votes"), "vote count", "SC") != method_total:
                    raise ValueError(f"{race}: SC county votes disagree with a method total")
            if sum(clarity.number(t.get("votes"), "vote count", "SC") for t in types) != votes:
                raise ValueError(f"{race}: SC vote methods disagree with candidate total")
            labels.append(label)
            rows.append(row(race, key, label, name, affiliation, votes))
            for place, votes_here in by_county.items():
                done, total = precincts[place]
                rows.append(
                    row(
                        race,
                        key,
                        label,
                        name,
                        affiliation,
                        votes_here,
                        county=place,
                        reporting=done,
                        total=total,
                    )
                )
        verify_scope("SC", race, labels, set(precincts))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        source_timestamp=text(root.findtext("Timestamp"), "timestamp"),
        official_results=final,
        contest_total_reconciliation="passed",
        vote_method_reconciliation="passed",
    )


def parse_workbook(archive_path, source, settings, output, stats, reference):
    with zipfile.ZipFile(archive_path) as archive:
        report_url = latest_report(archive.read("index.html").decode("utf-8-sig"), source)
        book = Workbook(archive.read(source.primary_filename), settings)
    rows, seen = [], set()
    try:
        toc = book.rows("Table of Contents")
        for entry in toc:
            if len(entry) != 2 or not isinstance(entry[1], str):
                continue
            race = race_name(entry[1])
            if race not in state_adapter("SC").races:
                continue
            if race in seen:
                raise ValueError("Duplicate SC contest in workbook index")
            seen.add(race)
            sheet = str(count(entry[0]))
            table = book.rows(sheet)
            if len(table) < 5 or text(table[0][0]) != text(entry[1]):
                raise ValueError("SC index and worksheet disagree")
            labels, header = table[1], table[2]
            if (
                header[:2] != ["County", "Registered Voters"]
                or header[-1] != "Total"
                or (len(header) - 3) % 7
            ):
                raise ValueError("Unexpected SC workbook header")
            candidates = []
            for start in range(2, len(header) - 1, 7):
                label = text(labels[start], "SC candidate")
                if tuple(header[start : start + 7]) != METHODS:
                    raise ValueError("Unexpected SC vote-method columns")
                match = re.fullmatch(r"([A-Z]{3}) (.+)", label)
                if match:
                    name, affiliation = match[2], party(match[1])
                elif label == "Write-In":
                    name, affiliation = label, ""
                else:
                    raise ValueError("Unexpected SC candidate label")
                candidates.append((start, label, name, affiliation))
            counties, official, candidate_sums = set(), False, {}
            for values in table[3:]:
                if len(values) != len(header):
                    raise ValueError("Unexpected SC workbook row width")
                county = text(values[0])
                is_total = county == "Total:"
                if official or (not is_total and county in counties):
                    raise ValueError("Duplicate SC county or misplaced official total")
                if not is_total:
                    counties.add(county)
                official = is_total
                total = 0
                for start, label, name, affiliation in candidates:
                    methods = [count(v) for v in values[start : start + 7]]
                    if sum(methods[:6]) != methods[6]:
                        raise ValueError("SC vote methods disagree with candidate total")
                    total += methods[6]
                    rows.append(
                        row(
                            race,
                            sheet,
                            label,
                            name,
                            affiliation,
                            methods[6],
                            county="" if is_total else county.upper(),
                        )
                    )
                    if is_total:
                        if candidate_sums.get(label) != methods:
                            raise ValueError(
                                "SC county/method sums disagree with official candidate total"
                            )
                    else:
                        sums = candidate_sums.setdefault(label, [0] * 7)
                        for i, number in enumerate(methods):
                            sums[i] += number
                if total != count(values[-1]):
                    raise ValueError("SC candidate sum disagrees with county/contest total")
            if (
                not official
                or not counties
                or (race in {"SC-SEN", "SC-GOV"} and len(counties) != 46)
            ):
                raise ValueError("Missing SC county coverage or official total")
            verify_scope("SC", race, [c[1] for c in candidates], {c.upper() for c in counties})
    finally:
        book.close()
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_report_url=report_url,
        contest_total_reconciliation="passed",
        vote_method_reconciliation="passed",
        source_update_text=report_url.rsplit("/", 1)[-1],
    )
