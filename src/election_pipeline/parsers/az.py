"""Arizona Secretary of State: the Enhanced Voting results export, saved by hand.

The operator saves the Secretary of State's results export,
``export-110326GeneralElection.json`` (from app.enhancedvoting.com), and imports it as a
manual source (docs/manual-results.md). It holds the election's statewide ``results``
and, in ``localResults``, the same contests for each of the 15 counties, with precinct
detail that is not used here.

Each selected contest (Governor, U.S. House 1-9) is checked: every county's candidates
add up, candidate by candidate, to the statewide counts, and each candidate keeps one ID
and name. The two generic write-in lines ("Unofficial Write-in", "Unassigned Write-in")
are kept as one "Write-in" line; named write-in candidates keep their names. The export
splits every count into five groups (``GROUPS``), stored as vote methods under Arizona's
own names: its "Early Voting" is its early ballots, not in-person early voting. Each
candidate's groups add up to its count, and the counties' to the statewide ones, group by
group. Its ``createdAt`` is the report time.
"""

import re
import zipfile
from collections import Counter
from datetime import datetime, timezone

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, unique_json

ELECTION = ("2026-11-03", "2026 General Election")
GOVERNOR = "Governor and Lieutenant Governor"
HOUSE = re.compile(r"U\.S\. Representative in Congress - District No\. (\d)")
PARTIES = {
    "Democratic": "DEM",
    "Republican": "REP",
    "Libertarian": "LIB",
    "Green": "GRN",
    "Independent": "IND",
    "No Labels": "NOL",
    "Other": "OTH",
}
GROUPS = {
    "Early Voting": "early_ballot",
    "Early A.R.S. 16-579": "early_ars_16_579",
    "Late Early*": "late_early",
    "Election Day": "election_day",
    "Provisional": "provisional",
}
WRITE_INS = {"Unofficial Write-in", "Unassigned Write-in"}
WRITE_IN = ("write-in", "Write-in")
COUNTIES = (
    "Apache", "Cochise", "Coconino", "Gila", "Graham", "Greenlee", "La Paz", "Maricopa",
    "Mohave", "Navajo", "Pima", "Pinal", "Santa Cruz", "Yavapai", "Yuma",
)  # fmt: skip


def race_name(name):
    if name == GOVERNOR:
        return "AZ-GOV"
    match = HOUSE.fullmatch(name)
    return f"AZ-{int(match[1]):02}" if match else None


def clean(name):
    """A candidate's name without the export's trailing spaces or a ticket's party tags."""
    name = re.sub(r" \([A-Z]{2,4}\)", "", " ".join(str(name).split()))
    if not name:
        raise ValueError("Arizona candidate without a name")
    return name


def contests(items, wanted, label):
    found = {}
    for item in items:
        if not isinstance(item, dict) or item.get("contestType") != "Candidate":
            continue
        race = race_name(item.get("name"))
        if race is None:
            continue
        if race not in wanted or race in found or item.get("voteFor") != 1:
            raise ValueError(f"{label}: unexpected or repeated Arizona contest {race}")
        found[race] = item
    return found


def methods(option, race, label):
    """{method: votes} from one option's groups, which add up to its count."""
    groups = option.get("groupResults")
    names = [g.get("groupName") for g in groups] if isinstance(groups, list) else []
    if sorted(names) != sorted(GROUPS) or any(g.get("isFromVirtualPrecinct") for g in groups):
        raise ValueError(f"{race} {label}: vote groups differ from the reviewed set")
    found = {GROUPS[g["groupName"]]: count(g["voteCount"]) for g in groups}
    if sum(found.values()) != count(option["voteCount"]):
        raise ValueError(f"{race} {label}: vote groups do not add up to the count")
    return found


def options(item, race, label, statewide=None):
    """{candidate ID: (name, party, votes, methods)}, write-in lines combined under WRITE_IN.

    A county's party labels vary ("REP", "No Labels Party", "NON" for "Other"), so a
    county's candidates take the statewide party, matched by ID and name.
    """
    found, write_ins = {}, None
    for option in item["ballotOptions"]:
        identity, votes = str(option["id"]), count(option["voteCount"])
        name = " ".join(str(option["name"]).split())
        split = methods(option, race, label)
        if name in WRITE_INS:
            if option.get("politicalParty") is not None:
                raise ValueError(f"{race} {label}: a write-in line with a party")
            write_ins = split if write_ins is None else {k: write_ins[k] + split[k] for k in split}
            continue
        if statewide is None:
            party = PARTIES.get(option.get("politicalParty"))
        elif identity in statewide and statewide[identity][0] == clean(name):
            party = statewide[identity][1]
        else:
            raise ValueError(f"{race} {label}: candidate differs from statewide")
        if party is None or identity in found:
            raise ValueError(f"{race} {label}: unexpected Arizona candidate {name!r}")
        found[identity] = (clean(name), party, votes, split)
    if write_ins is not None:
        found[WRITE_IN[0]] = (WRITE_IN[1], "", sum(write_ins.values()), write_ins)
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "AZ":
        raise ValueError("Unsupported Arizona source/election")
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo(source.primary_filename)
        if info.file_size > settings.max_uncompressed_bytes:
            raise ValueError("Arizona export is too large")
        data = unique_json(archive.read(info))
    if (data.get("electionDate"), data.get("electionName")) != ELECTION:
        raise ValueError("Arizona export belongs to another election")
    if data["results"].get("name") != "Arizona":
        raise ValueError("Arizona export lacks its statewide results")
    created = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*Z$", r"\1+00:00", data["createdAt"]))
    if created.tzinfo is None or created < datetime(2026, 11, 3, tzinfo=timezone.utc):
        raise ValueError("Arizona export has an unexpected creation time")
    wanted = state_adapter("AZ").races
    statewide = contests(data["results"]["ballotItems"], wanted, "statewide")
    if set(statewide) != set(wanted):
        raise ValueError("Arizona export lacks a selected contest")
    candidates = {race: options(item, race, "statewide") for race, item in statewide.items()}
    # Each candidate's county counts, in total and by method, summed across counties.
    sums = {race: {identity: Counter() for identity in found} for race, found in candidates.items()}
    places, rows = {race: set() for race in wanted}, []
    seen_counties = set()
    for local in data["localResults"]:
        match = re.fullmatch(r"(.+) County", str(local.get("name")))
        if not match or match[1] not in COUNTIES or match[1] in seen_counties:
            raise ValueError(f"Arizona export: unexpected county {local.get('name')!r}")
        county = match[1]
        seen_counties.add(county)
        for race, item in contests(local["ballotItems"], wanted, county).items():
            places[race].add(county)
            for identity, (name, party, votes, split) in options(
                item, race, county, candidates[race]
            ).items():
                sums[race][identity].update({"total": votes, **split})
                rows.append({**row(race, race, identity, name, party, votes, county), **split})
    if seen_counties != set(COUNTIES) or places["AZ-GOV"] != set(COUNTIES):
        raise ValueError("Arizona export lacks a county")
    for race, found in candidates.items():
        for identity, (name, party, votes, split) in found.items():
            if sums[race][identity] != Counter({"total": votes, **split}):
                raise ValueError(f"{race}: counties do not add up to the statewide counts")
            rows.append({**row(race, race, identity, name, party, votes), **split})
        names = [name for identity, (name, *_) in found.items() if identity != WRITE_IN[0]]
        verify_scope("AZ", race, names, places[race])
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=data["createdAt"],
        source_generated_at=created.isoformat(),
        contest_total_reconciliation="passed",
    )
