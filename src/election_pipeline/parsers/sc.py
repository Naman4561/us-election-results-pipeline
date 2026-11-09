"""SC's official county workbook fallback, with method and county reconciliation."""

import re
import zipfile

from election_pipeline.collectors.sc import latest_report
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, party, row, text
from election_pipeline.parsers.workbook import Workbook
from election_pipeline.states import state_adapter

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


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported South Carolina source/election")
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
