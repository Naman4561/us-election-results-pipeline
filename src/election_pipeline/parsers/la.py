"""Louisiana Secretary of State results (voterportal.sos.la.gov): totals by parish.

A bundle holds the documents the official "Live Election Results" page loads for the
November 3, 2026 election: ``dates.json`` (the election list, with its
``ResultsOfficial`` flag), ``races.json`` (contest and candidate names and IDs),
``votes.json`` (statewide candidate totals) and one ``parish-<race>.json`` per contest
(its parish breakdown). Parish counts must add up to the statewide totals exactly. The
documents carry no shared publication stamp, so a collection that straddles an update
fails that reconciliation and is retried, rather than paired.

Parishes are given by Louisiana's own parish numbers. ``PARISHES`` is the table the
official results page uses to name them (its ``parishList``). Louisiana elects a
senator and six U.S. House members in 2026; no governor. Counts are totals only.
"""

import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, text, unique_json

ELECTION_ID = "344"
ORDINALS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6}
PARISHES = {
    "01": "Acadia",
    "02": "Allen",
    "03": "Ascension",
    "04": "Assumption",
    "05": "Avoyelles",
    "06": "Beauregard",
    "07": "Bienville",
    "08": "Bossier",
    "09": "Caddo",
    "10": "Calcasieu",
    "11": "Caldwell",
    "12": "Cameron",
    "13": "Catahoula",
    "14": "Claiborne",
    "15": "Concordia",
    "16": "De Soto",
    "17": "East Baton Rouge",
    "18": "East Carroll",
    "19": "East Feliciana",
    "20": "Evangeline",
    "21": "Franklin",
    "22": "Grant",
    "23": "Iberia",
    "24": "Iberville",
    "25": "Jackson",
    "26": "Jefferson",
    "27": "Jefferson Davis",
    "28": "Lafayette",
    "29": "Lafourche",
    "30": "Lasalle",
    "31": "Lincoln",
    "32": "Livingston",
    "33": "Madison",
    "34": "Morehouse",
    "35": "Natchitoches",
    "36": "Orleans",
    "37": "Ouachita",
    "38": "Plaquemines",
    "39": "Pointe Coupee",
    "40": "Rapides",
    "41": "Red River",
    "42": "Richland",
    "43": "Sabine",
    "44": "St. Bernard",
    "45": "St. Charles",
    "46": "St. Helena",
    "47": "St. James",
    "48": "St. John The Baptist",
    "49": "St. Landry",
    "50": "St. Martin",
    "51": "St. Mary",
    "52": "St. Tammany",
    "53": "Tangipahoa",
    "54": "Tensas",
    "55": "Terrebonne",
    "56": "Union",
    "57": "Vermilion",
    "58": "Vernon",
    "59": "Washington",
    "60": "Webster",
    "61": "West Baton Rouge",
    "62": "West Carroll",
    "63": "West Feliciana",
    "64": "Winn",
}


def member(race):
    return f"parish-{race.lower()}.json"


def race(title):
    if title == "U. S. Senator":
        return "LA-SEN"
    match = re.fullmatch(
        r"U\. S\. Representative -- ([1-6](?:st|nd|rd|th)) Congressional District", title
    )
    return f"LA-0{ORDINALS[match[1]]}" if match and match[1] in ORDINALS else None


def listed(value, label):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return value
    raise ValueError(f"Unexpected Louisiana {label}")


def document(archive, name, key):
    value = unique_json(archive.read(name).decode("utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {key}:
        raise ValueError(f"Unexpected Louisiana {name}")
    return value[key]


def official(dates):
    found = [
        d for d in listed(dates["Date"], "election dates") if d.get("PKElectionID") == ELECTION_ID
    ]
    if len(found) != 1 or found[0].get("ElectionDate") != "11/03/2026":
        raise ValueError("Louisiana election list lacks the November 3, 2026 election")
    flag = found[0].get("ResultsOfficial")
    if flag not in ("0", "1"):
        raise ValueError("Unexpected Louisiana official flag")
    return flag == "1"


def candidate(desc):
    match = re.fullmatch(r"(.+) \(([A-Z]+)\)", " ".join(text(desc, "candidate").split()))
    if not match:
        raise ValueError(f"Unexpected Louisiana candidate {desc!r}")
    return match[1], match[2]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "LA":
        raise ValueError("Unsupported Louisiana source/election")
    wanted = state_adapter("LA").races
    with zipfile.ZipFile(archive_path) as archive:
        final = official(document(archive, "dates.json", "Dates"))
        races = document(archive, "races.json", "Races")
        votes = document(archive, "votes.json", "Races")
        breakdowns = {r: document(archive, member(r), "Parishes") for r in wanted}
    contests = {}
    for record in listed(races["Race"], "races"):
        contest = race(" ".join(text(record.get("SpecificTitle"), "contest").split()))
        if contest not in wanted:
            continue
        if contest in contests or record.get("NumberToBeElected") != "1":
            raise ValueError(f"{contest}: unexpected or duplicate Louisiana contest")
        names = {}
        for choice in listed(record["Choice"], "candidates"):
            identity = text(choice.get("ID"), "candidate ID")
            if identity in names:
                raise ValueError(f"{contest}: duplicate Louisiana candidate")
            names[identity] = candidate(choice.get("Desc"))
        contests[contest] = (text(record.get("ID"), "contest ID"), names)
    if set(contests) != set(wanted):
        raise ValueError("Louisiana results lack a selected contest")
    totals = {}
    for record in listed(votes["Race"], "vote totals"):
        totals.setdefault(record.get("ID"), []).append(record)
    rows = []
    for contest, (identity, names) in sorted(contests.items()):
        found = totals.get(identity, [])
        if len(found) != 1:
            raise ValueError(f"{contest}: missing or duplicate Louisiana totals")
        done, total = (
            count(int(found[0]["PrecinctsReporting"])),
            count(int(found[0]["PrecinctsExpected"])),
        )
        if done > total:
            raise ValueError(f"{contest}: more precincts reporting than exist")
        statewide = {}
        for choice in listed(found[0]["Choice"], "statewide candidates"):
            key = text(choice.get("ID"), "candidate ID")
            if key not in names or key in statewide:
                raise ValueError(f"{contest}: unexpected or duplicate candidate")
            statewide[key] = count(int(choice["VoteTotal"]))
        if set(statewide) != set(names):
            raise ValueError(f"{contest}: statewide totals lack a candidate")
        for key, votes_ in statewide.items():
            name, party = names[key]
            rows.append(
                row(contest, identity, key, name, party, votes_, reporting=done, total=total)
            )
        places = []
        for parish in listed(breakdowns[contest]["Parish"], "parishes"):
            place = PARISHES.get(parish.get("ParishValue"))
            if place is None or place in places:
                raise ValueError(f"{contest}: unknown or duplicate Louisiana parish")
            places.append(place)
            seen = {}
            for choice in listed(parish["Choice"], "parish candidates"):
                key = text(choice.get("ID"), "candidate ID")
                if key not in statewide or key in seen:
                    raise ValueError(f"{contest} {place}: unexpected or duplicate candidate")
                seen[key] = count(int(choice["VoteTotal"]))
            if set(seen) != set(statewide):
                raise ValueError(f"{contest} {place}: parish lacks a candidate")
            for key, votes_ in seen.items():
                name, party = names[key]
                rows.append(row(contest, identity, key, name, party, votes_, county=place))
        verify_scope("LA", contest, list(statewide), places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=text(races.get("WriteTime"), "update time"),
        official_results=final,
        contest_total_reconciliation="passed",
    )
