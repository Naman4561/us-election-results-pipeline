"""Shared reader for official Tally election-night results (Arkansas, North Dakota).

Tally (KnowInk's Total Results) serves each state's results page from a public API. A
bundle holds the documents the page itself loads for one election:

- ``info.json`` (``Election/GetElectionInfo``): the election, its parties and its
  locations, whose county entries (keyed by county FIPS) give county names;
- ``contests.json`` (``Contest/GetContestSearchList``): contest and candidate names;
- one ``results-<type>.json`` (``Contest/GetContestResults``) per contest type holding a
  selected contest: candidate totals statewide and by county.

Every document repeats the election ID, the publication version and whether the results
are official; all must agree, so a bundle never pairs two publications. For each selected
contest, candidate counts must add up to the contest total and county counts to the
statewide counts. The site does not split these totals by ballot type in a way that is
stored here; counts are totals only.
"""

import re
import zipfile
from dataclasses import dataclass

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, text, unique_json

INFO = "info.json"
CONTESTS = "contests.json"
PARTIES = {
    "Republican": "REP",
    "Democratic": "DEM",
    "Democratic-NPL": "DEM",
    "Libertarian": "LIB",
    "Green": "GRN",
    "Independent": "IND",
    "independent nomination": "IND",
    "Constitution": "CON",
    "American Delta": "ADP",
    "Write-In": "W",
}


@dataclass(frozen=True)
class Site:
    state: str
    name: str
    fips: str  # the state's two-digit FIPS code, prefixing its county location IDs
    election_id: str
    election_name: str
    race: object  # contest name -> contest ID or None
    types: dict  # contest ID -> contest type code, naming its results member


def results_member(kind):
    return f"results-{kind.lower()}.json"


def envelope(raw, site, label):
    """A document's payload, after checking it belongs to the configured election."""
    value = unique_json(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or value.get("errorMessage") is not None:
        raise ValueError(f"{site.name} {label} reports an error")
    if text(value.get("electionID"), "election ID") != site.election_id:
        raise ValueError(f"{site.name} {label} belongs to another election")
    official = value.get("isOfficial")
    if type(official) is not bool:
        raise ValueError(f"Unexpected {site.name} official flag")
    return value, (text(value.get("versionID"), "version"), official)


def party(record, parties, site):
    found = parties.get(record.get("partyID"))
    if not isinstance(found, dict):
        raise ValueError(f"{site.name} candidate has an unknown party")
    return PARTIES.get(found.get("partyName"), text(found.get("partyName"), "party").upper())


def candidate_name(name, code):
    """The published name, less a leading party code ("REP Tom Cotton") equal to its own."""
    name = " ".join(text(name, "candidate").split())
    return name[len(code) + 1 :] if name.startswith(code + " ") else name


def parse_site(site, archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError(f"Unsupported {site.name} source/election")
    wanted = state_adapter(site.state).races
    if set(site.types) != set(wanted):
        raise ValueError(f"{site.name} contest types do not cover its races")
    versions = set()
    with zipfile.ZipFile(archive_path) as archive:
        info, version = envelope(archive.read(INFO), site, "election")
        versions.add(version)
        listing, version = envelope(archive.read(CONTESTS), site, "contest list")
        versions.add(version)
        results = {}
        for kind in sorted(set(site.types.values())):
            results[kind], version = envelope(archive.read(results_member(kind)), site, kind)
            versions.add(version)
    if len(versions) != 1:
        raise ValueError(f"{site.name} documents come from different publications")
    ((published, official),) = versions
    election = info["response"]
    if (
        text(election.get("name"), "election name") != site.election_name
        or election.get("date") != "2026-11-03T00:00:00"
    ):
        raise ValueError(f"{site.name} election belongs to another date or name")
    parties = election["parties"]
    counties = {}
    for key, location in election["locations"].items():
        if re.fullmatch(site.fips + r"[0-9]{3}", key):
            if location.get("locationID") != key:
                raise ValueError(f"{site.name} location is mislabeled")
            counties[key] = " ".join(text(location.get("locationName"), "county").split())
    names = {}
    for identity, contest in listing["response"]["contests"].items():
        race = site.race(" ".join(text(contest.get("contestName"), "contest").split()))
        if race not in wanted:
            continue
        if (
            race in names
            or contest.get("contestId") != identity
            or contest.get("voteFor") != 1
            or contest.get("contestTypeCode") != site.types[race]
        ):
            raise ValueError(f"{race}: unexpected or duplicate {site.name} contest")
        names[race] = (identity, contest["choices"])
    if set(names) != set(wanted):
        raise ValueError(f"{site.name} results lack a selected contest")
    rows = []
    for race, (identity, choices) in sorted(names.items()):
        contest = results[site.types[race]]["response"]["contests"].get(identity)
        if (
            not isinstance(contest, dict)
            or contest.get("contestID") != identity
            or contest.get("writeInChoices")
            or contest.get("hasRecount") is not False
        ):
            raise ValueError(f"{race}: missing or unexpected {site.name} contest results")
        labels = {}
        for choice_id, choice in choices.items():
            if choice.get("id") != choice_id:
                raise ValueError(f"{race}: mislabeled {site.name} candidate")
            code = party(choice, parties, site)
            labels[choice_id] = (candidate_name(choice.get("name"), code), code)
        statewide = {}
        for choice in contest["choices"]:
            choice_id = text(choice.get("choiceID"), "candidate ID")
            if choice_id not in labels or choice_id in statewide:
                raise ValueError(f"{race}: unexpected or duplicate {site.name} candidate")
            statewide[choice_id] = count(choice["totalVotes"])
        if set(statewide) != set(labels) or sum(statewide.values()) != count(contest["totalVotes"]):
            raise ValueError(f"{race}: {site.name} candidate counts disagree with the total")
        done, total = count(contest["precinctsReporting"]), count(contest["totalPrecincts"])
        if done > total:
            raise ValueError(f"{race}: more precincts reporting than exist")
        for choice_id, votes in statewide.items():
            name, code = labels[choice_id]
            rows.append(
                row(race, identity, choice_id, name, code, votes, reporting=done, total=total)
            )
        places = []
        for key, local in contest["locations"].items():
            if key not in counties or local.get("locationId") != key:
                raise ValueError(f"{race}: {site.name} result for an unknown county")
            county = counties[key]
            places.append(county)
            seen = {}
            for choice in local["choices"]:
                choice_id = text(choice.get("choiceID"), "candidate ID")
                if choice_id not in statewide or choice_id in seen:
                    raise ValueError(f"{race} {county}: unexpected or duplicate candidate")
                seen[choice_id] = count(choice["totalVotes"])
            if set(seen) != set(statewide) or sum(seen.values()) != count(local["totalVotes"]):
                raise ValueError(f"{race} {county}: county counts disagree with the total")
            for choice_id, votes in seen.items():
                name, code = labels[choice_id]
                rows.append(row(race, identity, choice_id, name, code, votes, county=county))
        verify_scope(site.state, race, list(statewide), places)
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_version=published,
        source_update_text=text(info.get("lastUpdated"), "update time"),
        official_results=official,
        contest_total_reconciliation="passed",
        vote_methods="reported_not_stored",
    )
