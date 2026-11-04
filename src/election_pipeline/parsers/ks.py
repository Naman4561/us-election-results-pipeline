"""Kansas's official page, including the county tables embedded in map tooltips."""

import re
import zipfile
from datetime import datetime

from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import formatted_count, party, row
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401
from election_pipeline.states import state_adapter


def race_name(name):
    if name in {"United States Senate", "Governor / Lt. Governor"}:
        return {"United States Senate": "KS-SEN", "Governor / Lt. Governor": "KS-GOV"}[name]
    match = re.fullmatch(r"United States House of Representatives (\d+)", name)
    return f"KS-{int(match[1]):02}" if match else None


def candidate(raw):
    match = re.fullmatch(r"([A-Z])-\s*(.+)", raw)
    if not match:
        raise ValueError("Unexpected Kansas party/candidate label")
    return match[2], party(match[1])


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported Kansas source/election")
    with zipfile.ZipFile(archive_path) as archive:
        tree = Tree(archive.read(source.primary_filename).decode("utf-8-sig")).root
    keywords = [
        n.attrs.get("content", "")
        for n in tree.find("meta")
        if n.attrs.get("name", "").lower() == "keywords"
    ]
    timestamps = tree.find(identity="time-stamp")
    tabs = tree.find(identity="party-all")
    if (
        len(keywords) != 1
        or "2026 General Election Results" not in keywords[0]
        or len(timestamps) != 1
        or len(tabs) != 1
    ):
        raise ValueError("Kansas page belongs to another election or is not a results page")
    updated = datetime.strptime(timestamps[0].text, "%m/%d/%Y %I:%M %p")
    if not datetime(2026, 11, 3) <= updated <= datetime(2026, 12, 2):
        raise ValueError("Kansas update date is outside this election's reporting window")
    rows, seen = [], set()
    for block in tabs[0].find("div", css="race"):
        headings = block.find("h3")
        if len(headings) != 1:
            raise ValueError("Unexpected Kansas race heading")
        race = race_name(headings[0].text)
        if race not in state_adapter("KS").races:
            continue
        if race in seen:
            raise ValueError("Duplicate Kansas contest")
        seen.add(race)
        ids = [
            n.attrs["id"] for n in block.find("input") if n.attrs.get("name") == "chkbxFavoriteRace"
        ]
        subheading = block.find(css="race-subheading")
        lists = block.find(css="race-candidate-list")
        if len(ids) != 1 or len(subheading) != 1 or len(lists) != 1:
            raise ValueError("Missing Kansas race metadata")
        match = re.search(r"Precincts Reporting: (\d+) of (\d+)", subheading[0].text)
        if not match:
            raise ValueError("Missing Kansas precinct counts")
        reporting, total = map(int, match.groups())
        if reporting > total:
            raise ValueError("Kansas reporting exceeds total precincts")
        official, reported_total = {}, None
        for result in lists[0].find("div", css="row"):
            labels, counts = result.find(css="col-6"), result.find(css="col-2")
            if not labels and not counts:
                continue
            if len(labels) != 1 or len(counts) != 1:
                raise ValueError("Unexpected Kansas candidate row")
            label, votes = labels[0].text, formatted_count(counts[0].text)
            if label == "Total Votes":
                if reported_total is not None:
                    raise ValueError("Duplicate Kansas race total")
                reported_total = votes
                continue
            name, affiliation = candidate(label)
            if label in official:
                raise ValueError("Duplicate Kansas candidate")
            official[label] = (name, affiliation)
            rows.append(
                row(race, ids[0], label, name, affiliation, votes, reporting=reporting, total=total)
            )
        if (
            reported_total is None
            or sum(r["total_votes"] for r in rows if r["contest_id"] == race) != reported_total
        ):
            raise ValueError("Kansas candidate sum disagrees with race total")
        areas = block.find("area")
        counties = set()
        for area in areas:
            county = area.attrs.get("alt", "").strip().upper()
            if not county or county in counties:
                raise ValueError("Missing/duplicate Kansas map county")
            counties.add(county)
            tooltip = Tree(area.attrs.get("title", "")).root
            county_labels = tooltip.find(css="county")
            if len(county_labels) != 1 or county_labels[0].text.upper() != county + " COUNTY":
                raise ValueError("Kansas tooltip county differs from its map label")
            if tooltip.text.endswith("No results reported"):
                # No candidate rows means unknown/unreported, never inferred zero votes.
                continue
            precincts = re.search(r"(\d+) of (\d+) precincts reporting", tooltip.text)
            if not precincts:
                raise ValueError("Unexpected Kansas county reporting text")
            cr, ct = map(int, precincts.groups())
            if cr > ct:
                raise ValueError("Kansas county reporting exceeds total precincts")
            found = set()
            for line in tooltip.find("tr"):
                cells = line.find("td")
                if len(cells) != 2 or cells[0].text == "Candidate":
                    continue
                label = cells[0].text
                if label not in official or label in found:
                    raise ValueError("Unexpected/duplicate Kansas county candidate")
                found.add(label)
                name, affiliation = official[label]
                rows.append(
                    row(
                        race,
                        ids[0],
                        label,
                        name,
                        affiliation,
                        formatted_count(cells[1].text),
                        county=county,
                        reporting=cr,
                        total=ct,
                    )
                )
            if found != set(official):
                raise ValueError("Missing Kansas county candidate")
        if not counties or (race in {"KS-SEN", "KS-GOV"} and len(counties) != 105):
            raise ValueError("Kansas map must identify all 105 counties, including unreported ones")
        verify_scope("KS", race, list(official), counties)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(source_update_text=timestamps[0].text, contest_total_reconciliation="passed")
