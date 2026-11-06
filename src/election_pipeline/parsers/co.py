"""Colorado Secretary of State results (Clarity): totals by county.

``detail.zip`` is the site's "Detail XML" report: each choice's votes by county. Colorado
votes by mail and reports a single "Total Votes" type, so this is a totals-only feed.
Each county's candidate counts must add up to the statewide count, which must equal the
official summary. Governor tickets are shown as "Governor / Lieutenant Governor", as
published. The summary writes nicknames with single quotes where the detail report uses
double quotes (``'Marky Jr'`` / ``"Marky Jr"``); names are compared with quotes folded and
stored as the detail report prints them.
"""

import re
import zipfile

from election_pipeline.parsers import clarity
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row, text, xml
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

VERSION_FILES = {
    "summary.json": "json/en/summary.json",
    "settings.json": "json/en/electionsettings.json",
    "detail.zip": "reports/detailxml.zip",
}


def race(name):
    if name == "United States Senator":
        return "CO-SEN"
    if name == "Governor / Lieutenant Governor":
        return "CO-GOV"
    match = re.fullmatch(
        r"Representative to the 120th United States Congress - District ([1-8])", name
    )
    return f"CO-0{match[1]}" if match else None


def same_name(detail, summary):
    def fold(name):
        return name.replace("''", '"').replace("'", '"')

    return fold(detail) == fold(summary)


def number(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,11}", value):
        raise ValueError(f"Invalid Colorado {label}")
    return int(value)


def contests(root, wanted):
    if (
        text(root.findtext("ElectionName"), "election name") != "2026 General"
        or text(root.findtext("ElectionDate"), "election date") != "11/3/2026"
        or text(root.findtext("Region"), "region") != "CO"
    ):
        raise ValueError("Colorado detail report belongs to another election")
    found = {}
    for contest in root.findall("Contest"):
        identity = race(text(contest.get("text"), "contest name"))
        if identity not in wanted:
            continue
        if (
            identity in found
            or contest.get("voteFor") != "1"
            or contest.get("isQuestion") != "false"
        ):
            raise ValueError(f"{identity}: unexpected or duplicate Colorado contest")
        found[identity] = contest
    if set(found) != set(wanted):
        raise ValueError("Colorado detail report lacks a selected contest")
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "CO":
        raise ValueError("Unsupported Colorado source/election")
    wanted = state_adapter("CO").selected_races(source)
    with zipfile.ZipFile(archive_path) as archive:
        updated = clarity.settings_check(archive.read("settings.json"), "CO", settings)
        official = clarity.summary(archive.read("summary.json"), race, settings)
        root = xml(clarity.detail_member(archive.read("detail.zip"), settings, "xml"))
    found = contests(root, wanted)
    if set(official) != set(wanted):
        raise ValueError("Colorado summary lacks a selected contest")
    rows = []
    for identity, contest in found.items():
        key, expected = official[identity]
        if contest.get("key") != key:
            raise ValueError(f"{identity}: detail and summary contest keys differ")
        precincts = {}
        for county in contest.findall("ParticipatingCounties/County"):
            name = text(county.get("name"), "county")
            done = number(county.get("precinctsReported"), "precinct count")
            total = number(county.get("precinctsParticipating"), "precinct count")
            if name in precincts or done > total:
                raise ValueError(f"{identity}: invalid county reporting status")
            precincts[name] = (done, total)
        choices = contest.findall("Choice")
        if len(choices) != len(expected):
            raise ValueError(f"{identity}: detail and summary candidates differ")
        candidates = []
        for choice, (listed, party, votes) in zip(choices, expected, strict=True):
            identity_key = f"{key}-{number(choice.get('key'), 'choice key')}"
            name = text(choice.get("text"), "candidate")
            if (
                not same_name(name, listed)
                or (choice.get("party") or "").strip() != party
                or number(choice.get("totalVotes"), "vote count") != votes
            ):
                raise ValueError(f"{identity}: detail and summary candidate totals differ")
            types = choice.findall("VoteType")
            if [t.get("name") for t in types] != ["Total Votes"]:
                raise ValueError(f"{identity}: unexpected Colorado vote types")
            if number(types[0].get("votes"), "vote count") != votes:
                raise ValueError(f"{identity}: vote type total differs from candidate total")
            candidates.append(identity_key)
            rows.append(row(identity, key, identity_key, name, party, votes))
            seen = set()
            for county in types[0].findall("County"):
                place = text(county.get("name"), "county")
                if place in seen or place not in precincts:
                    raise ValueError(f"{identity}: unexpected or duplicate county {place}")
                seen.add(place)
                done, total = precincts[place]
                rows.append(
                    row(
                        identity,
                        key,
                        identity_key,
                        name,
                        party,
                        number(county.get("votes"), "vote count"),
                        county=place,
                        reporting=done,
                        total=total,
                    )
                )
            if seen != set(precincts):
                raise ValueError(f"{identity}: candidate lacks a participating county")
        verify_scope("CO", identity, candidates, sorted(precincts))
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        source_timestamp=text(root.findtext("Timestamp"), "timestamp"),
        contest_total_reconciliation="passed",
        vote_methods="not_published",
    )
