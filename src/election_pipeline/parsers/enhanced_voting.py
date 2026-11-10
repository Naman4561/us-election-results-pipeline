"""Shared reader for official Enhanced Voting results (Virginia, Idaho, Washington).

A bundle holds the election's ``data`` document (statewide totals for every contest) and
one ``ballot-items/<id>`` document per selected contest (its county breakdown). For each
selected contest the parser checks:

- the election, jurisdiction and publication identifiers are the configured ones;
- candidate counts add up to the contest total;
- every county's candidate counts add up, per candidate, to the statewide count;
- where a state publishes vote methods, each candidate's methods add up to its count, in
  every county and statewide, and are stored; otherwise none may be present.

Each state module supplies a ``Site`` describing its race names, county names and method
groups. Nothing is estimated: a contest the site has not broken down by county fails the
parse and the previous publication stays live.
"""

import re
import zipfile
from dataclasses import dataclass, field

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, party, row, text, unique_json


@dataclass(frozen=True)
class Site:
    state: str
    name: str
    jurisdiction: str
    election_id: str
    vote_for: tuple
    race: object  # English contest name -> contest ID or None
    county: object  # English locality name -> stored county name
    methods: dict = field(default_factory=dict)  # English group name -> method column
    # English party name -> code, for a site that names parties without abbreviations.
    parties: dict = field(default_factory=dict)


def english(values, label="name"):
    if not isinstance(values, list):
        raise ValueError(f"Missing {label}")
    matches = [x.get("text") for x in values if isinstance(x, dict) and x.get("languageId") == "en"]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one English {label}")
    return text(matches[0], label)


def party_code(option, site, label):
    """The candidate's party code: the site's abbreviation, else its reviewed party name."""
    record = option.get("party") or {}
    abbreviation = record.get("abbreviation")
    if abbreviation or not site.parties or not record:
        return party(abbreviation or "")
    name = english(record.get("name"), "party name")
    if name not in site.parties:
        raise ValueError(f"{label}: party {name!r} is not in the reviewed list")
    return site.parties[name]


def member(race):
    return f"item-{race.lower()}.json"


def election(data, site, source):
    record = data["election"]
    if (
        source.state != site.state
        or record["electionDate"] != source.election_date
        or record["isPrimary"] is not False
        or record["isProduction"] is not True
        or data["jurisdiction"]["shortName"] != site.jurisdiction
    ):
        raise ValueError(f"Wrong {site.name} election or jurisdiction")
    return record


def selected(data, site, wanted):
    """Selected top-level candidate contests, keyed by contest ID."""
    found = {}
    for ballot in data["ballotItems"]:
        if not isinstance(ballot, dict) or ballot.get("contestType") != "Candidate":
            continue
        race = site.race(english(ballot["name"]))
        if race not in wanted:
            continue
        if (
            race in found
            or ballot["parentId"] is not None
            or ballot["publishPublicElectionId"] != site.election_id
            or (site.vote_for and english(ballot["voteFor"], "vote-for text") not in site.vote_for)
        ):
            raise ValueError(f"{race}: unexpected or duplicate {site.name} contest")
        found[race] = ballot
    if set(found) != set(wanted):
        raise ValueError(f"{site.name} results lack a selected contest")
    return found


def ballot_items(raw, source, site):
    from election_pipeline.states import state_adapter

    data = unique_json(raw)
    election(data, site, source)
    wanted = state_adapter(site.state).races
    return [(member(race), text(b["id"])) for race, b in selected(data, site, wanted).items()]


def methods(option, site, label):
    """Method counts for one candidate, or {} where the state publishes none."""
    groups = option.get("groupResults") or []
    if not site.methods:
        if groups:
            raise ValueError(f"{label}: unexpected vote-method detail")
        return {}
    names = [english(g["groupName"], "vote method") for g in groups]
    if sorted(names) != sorted(site.methods):
        raise ValueError(f"{label}: vote methods differ from the reviewed set")
    values = {site.methods[n]: count(g["voteCount"]) for n, g in zip(names, groups, strict=True)}
    if sum(values.values()) != count(option["voteCount"]):
        raise ValueError(f"{label}: vote methods do not add up to the candidate count")
    return values


def options(results, site, label):
    found = {}
    for option in results["ballotOptions"]:
        identity = text(option["id"], "candidate ID")
        if identity in found:
            raise ValueError(f"{label}: duplicate candidate")
        found[identity] = option
    return found


def reporting(status, label):
    if not isinstance(status, dict):
        raise ValueError(f"{label}: missing reporting status")
    done, total = count(status["reportingUnits"]), count(status["totalUnits"])
    if done > total:
        raise ValueError(f"{label}: more units reporting than exist")
    return done, total


def parse_site(site, archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError(f"Unsupported {site.name} source/election")
    wanted = state_adapter(site.state).races
    with zipfile.ZipFile(archive_path) as archive:
        data = unique_json(archive.read(source.primary_filename))
        record = election(data, site, source)
        found = selected(data, site, wanted)
        items = {race: unique_json(archive.read(member(race))) for race in found}
    rows = []
    for race, ballot in found.items():
        statewide = options(ballot["summaryResults"], site, race)
        if sum(count(o["voteCount"]) for o in statewide.values()) != count(ballot["voteTotal"]):
            raise ValueError(f"{race}: candidate counts disagree with the contest total")
        candidates = {}
        for identity, option in statewide.items():
            name = english(option["name"])
            candidates[identity] = (name, party_code(option, site, race))
            rows.append(
                {
                    **row(
                        race,
                        ballot["id"],
                        identity,
                        name,
                        candidates[identity][1],
                        option["voteCount"],
                    ),
                    **methods(option, site, race),
                }
            )
        item = items[race]
        breakdown = item["breakdownResults"]
        if item["id"] != ballot["id"] or not isinstance(breakdown, list):
            raise ValueError(f"{race}: county detail belongs to another contest")
        if item["breakdownResultCount"] != len(breakdown) or not breakdown:
            raise ValueError(f"{race}: county detail is incomplete")
        counties = set()
        for locality in breakdown:
            county = site.county(english(locality["locality"]["name"], "county name"))
            if county in counties:
                raise ValueError(f"{race}: duplicate county {county}")
            counties.add(county)
            done, total = reporting(locality["precinctReportingStatus"], f"{race} {county}")
            local = options(locality, site, f"{race} {county}")
            if not local.keys() <= statewide.keys():
                raise ValueError(f"{race} {county}: candidate missing from statewide results")
            if sum(count(o["voteCount"]) for o in local.values()) != count(locality["voteTotal"]):
                raise ValueError(f"{race} {county}: candidate counts disagree with county total")
            for identity, option in local.items():
                name, label = candidates[identity]
                if english(option["name"]) != name:
                    raise ValueError(f"{race} {county}: candidate name differs from statewide")
                rows.append(
                    {
                        **row(
                            race,
                            ballot["id"],
                            identity,
                            name,
                            label,
                            option["voteCount"],
                            county=county,
                            reporting=done,
                            total=total,
                        ),
                        **methods(option, site, f"{race} {county}"),
                    }
                )
        verify_scope(site.state, race, list(statewide), sorted(counties))
    finish(
        rows,
        source,
        settings,
        output,
        stats,
        reference=reference,
        reconcile=True,
    )
    stats.update(
        source_version=text(record["publishVersionId"]),
        source_update_text=text(record["asOf"]),
        official_results=record.get("isOfficialResults") is True,
        contest_total_reconciliation="passed",
    )


def strip_county(name):
    match = re.fullmatch(r"(.+?) County", name)
    if not match:
        raise ValueError(f"Unexpected county name {name!r}")
    return match[1]
