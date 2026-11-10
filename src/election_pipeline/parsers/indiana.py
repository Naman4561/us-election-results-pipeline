"""Indiana Election Division results: U.S. House totals by county.

A bundle holds the site's ``settings.json`` (the election, its publication version and
whether the results are certified) and ``house.json``, the "US Representative" file the
official page loads. Its statewide summary gives each district's candidate totals; its
district regions give every county's count for each candidate. Every county's counts
must add up to the statewide totals, or the snapshot is rejected.

The files are converted from XML: a list with one entry is written as that entry alone
(District 7 lies in Marion County only), so a single object is read as a list of one.
Indiana elects no senator or governor in 2026. Its site does not split counts by
ballot type. Write-in candidates are listed as published.

Candidates are identified by their published name, pinned by the roster: Indiana
renumbers its candidate IDs from one publication to the next (Nov 10, 2026). Within a
publication each name must keep one ID in every county.
"""

import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, row, text, unique_json

SETTINGS = "settings.json"
HOUSE = "house.json"
TITLE = re.compile(r"United States Representative, \(([1-9])\) District")


def listed(value, label):
    """An XML-converted repeated element: a list, or a single object for one entry."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        return value
    raise ValueError(f"Unexpected Indiana {label}")


def root(raw, label):
    value = unique_json(raw.decode("utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"Root"}:
        raise ValueError(f"Unexpected Indiana {label} document")
    return value["Root"]


def election(raw):
    """The settings' update time and whether the results are certified."""
    settings = root(raw, "settings")
    if (
        settings.get("CurrentElection") != "11/03/2026"
        or settings.get("ElectionType") != "G"
        or settings.get("StateFP") != "18"
    ):
        raise ValueError("Indiana settings belong to another election")
    certified = settings.get("Certified")
    if certified not in ("T", "F"):
        raise ValueError("Unexpected Indiana certified flag")
    return text(settings.get("WriteTime"), "update time"), certified == "T"


def race(record):
    match = TITLE.fullmatch(text(record.get("SubSortOrder"), "contest name"))
    if not match or record.get("NumofSeats") != "1":
        raise ValueError("Unexpected Indiana U.S. House contest")
    return f"IN-0{match[1]}"


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "IN":
        raise ValueError("Unsupported Indiana source/election")
    wanted = state_adapter("IN").races
    with zipfile.ZipFile(archive_path) as archive:
        updated, certified = election(archive.read(SETTINGS))
        house = root(archive.read(HOUSE), "House")
    category = house.get("OfficeCategory") or {}
    if category.get("OFFICECATEGORYID") != "1005":
        raise ValueError("Indiana file is not the US Representative office")
    statewide = {}
    for record in listed(house["StatewideSummary"]["Race"], "statewide races"):
        contest = race(record)
        if contest in statewide:
            raise ValueError(f"{contest}: duplicate Indiana contest")
        candidates = {}
        for candidate in listed(record["Candidates"]["Candidate"], "candidates"):
            name = " ".join(text(candidate["NAME_ON_BALLOT"], "candidate").split())
            if name in candidates:
                raise ValueError(f"{contest}: duplicate Indiana candidate")
            candidates[name] = (text(candidate["PARTY"], "party"), count(candidate["TOTAL"]))
        statewide[contest] = (text(record["OFFICEID"], "office ID"), candidates)
    if set(statewide) != set(wanted):
        raise ValueError("Indiana results lack a selected contest")
    rows, seen = [], set()
    for region in listed(category["Regions"]["Region"], "districts"):
        if region.get("MAP_JURISDICTION_TYPE") != "District":
            raise ValueError("Unexpected Indiana region")
        contest = f"IN-0{text(region.get('MAP_JURISDICTION_NAME'), 'district')}"
        if contest not in statewide or contest in seen:
            raise ValueError(f"Unexpected or duplicate Indiana district {contest}")
        seen.add(contest)
        office, candidates = statewide[contest]
        identities, counties = {}, []
        for local in listed(region["Races"]["Race"], "county contests"):
            place = local.get("Jurisdiction") or {}
            if (
                race(local) != contest
                or local.get("OFFICEID") != office
                or place.get("JURISDICTION_TYPE") != "Locality"
                or not re.fullmatch(r"18[0-9]{3}", place.get("FIPS") or "")
            ):
                raise ValueError(f"{contest}: unexpected Indiana county contest")
            county = text(place.get("JURISDICTION_NAME"), "county")
            if county in counties:
                raise ValueError(f"{contest}: duplicate Indiana county {county}")
            counties.append(county)
            names = set()
            for candidate in listed(local["Candidates"]["Candidate"], "county candidates"):
                name = " ".join(text(candidate["NAME_ON_BALLOT"], "candidate").split())
                identity = text(candidate["CANDIDATEID"], "candidate ID")
                if (
                    name not in candidates
                    or name in names
                    or identities.setdefault(name, identity) != identity
                    or text(candidate["PARTY_ABBREV"], "party") != candidates[name][0]
                ):
                    raise ValueError(f"{contest} {county}: candidate differs from the summary")
                names.add(name)
                rows.append(
                    row(
                        contest,
                        office,
                        name,
                        name,
                        candidates[name][0],
                        count(candidate["TOTAL_VOTES"]),
                        county=county,
                    )
                )
            if names != set(candidates):
                raise ValueError(f"{contest} {county}: county lacks a candidate")
        for name, (party, votes) in candidates.items():
            rows.append(row(contest, office, name, name, party, votes))
        verify_scope("IN", contest, list(identities), counties)
    if seen != set(wanted):
        raise ValueError("Indiana results lack a district's counties")
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        source_update_text=updated,
        official_results=certified,
        contest_total_reconciliation="passed",
    )
