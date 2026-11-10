"""Vermont Secretary of State results: totals by town.

A bundle holds the election's index (``index.json``: the election and whether its
results are official) and the two files it names for the publication: ``federal.json``
(Representative to Congress) and ``statewide.json`` (Governor). Each office lists a
"STATE WIDE" entry, the statewide count, and one entry per town and House district: a
town split among legislative districts appears once per district, and its entries are
added together. Every town's counts must add up to the statewide entry exactly.

Vermont reports by town, not county, so its map draws towns (Census county
subdivisions); towns are stored where other states store counties. Town names are the
map's: "SAINT" is written "St." and a town's "TOWN"/"CITY" suffix is dropped, except
where a city and a town share a name (Barre, Newport, Rutland, St. Albans). Write-in
votes, named or not, are kept as one "Write-in" line, as their names vary by town.
Vermont elects no senator in 2026. Counts are totals only.
"""

import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import count, text, unique_json
from election_pipeline.parsers.structured import row as result_row

OFFICES = {
    ("federal.json", "REPRESENTATIVE TO CONGRESS"): "VT-AL",
    ("statewide.json", "GOVERNOR"): "VT-GOV",
}
STATEWIDE = "STATE WIDE"
SHARED = {"BARRE", "NEWPORT", "RUTLAND", "ST. ALBANS"}
# The parties on Vermont's 2026 ballot for these offices; any other is refused. A
# candidate nominated by both Democrats and Progressives is listed "DEM/PROG".
PARTIES = {
    "DEMOCRATIC": "DEM",
    "DEM/PROG": "DEM",
    "REPUBLICAN": "REP",
    "INDEPENDENT": "IND",
    "PEACE AND JUSTICE": "PJ",
    "FREEDOM AND UNITY": "FU",
}
WRITE_IN = ("write-in", "Write-in")


def town(name):
    """A town's name as Vermont's map writes it."""
    name = " ".join(text(name, "town").upper().split())
    name = re.sub(r"^SAINT ", "ST. ", name)
    match = re.fullmatch(r"(.+) (TOWN|CITY)", name)
    if match and match[1] not in SHARED:
        name = match[1]
    return name.title()


def election(raw):
    index = unique_json(raw.decode("utf-8-sig"))
    details = index.get("electionDetails") or {}
    if (
        details.get("electionName") != "2026 GENERAL ELECTION"
        or details.get("electionDate") != "2026-11-03T00:00:00"
        or details.get("isStateWideElection") is not True
        or details.get("isGeneralElection") is not True
    ):
        raise ValueError("Vermont index belongs to another election")
    if type(details.get("isOfficial")) is not bool:
        raise ValueError("Unexpected Vermont official flag")
    return details["isOfficial"]


def tally(entry, label):
    """{candidate ID: (name, party, votes)}, write-ins combined into one line."""
    found = {}
    for candidate in entry.get("rc") or []:
        identity = str(count(candidate["cid"]))
        party = " ".join(text(candidate.get("pn"), "party").split())
        if identity in found or candidate.get("isWriteIn") or party not in PARTIES:
            raise ValueError(f"{label}: unexpected or duplicate Vermont candidate")
        name = " ".join(text(candidate["cn"], "candidate").split())
        found[identity] = (name, PARTIES[party], count(candidate["vc"]))
    found[WRITE_IN[0]] = (WRITE_IN[1], "", sum(count(c["vc"]) for c in entry.get("wc") or []))
    return found


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "VT":
        raise ValueError("Unsupported Vermont source/election")
    wanted = state_adapter("VT").races
    with zipfile.ZipFile(archive_path) as archive:
        official = election(archive.read("index.json"))
        documents = {
            m: unique_json(archive.read(m).decode("utf-8-sig"))
            for m in ("federal.json", "statewide.json")
        }
    rows = []
    for (member, title), race in OFFICES.items():
        offices = [o for part in documents[member]["d"] for o in part["o"] if o.get("on") == title]
        if len(offices) != 1 or offices[0].get("vf") != 1:
            raise ValueError(f"{race}: missing, duplicate or multi-seat Vermont office")
        office = offices[0]
        identity = str(count(office["oid"]))
        statewide, towns = None, {}
        for entry in office["cs"]:
            name = " ".join(text(entry.get("tn"), "town").split())
            if name == STATEWIDE:
                if statewide is not None:
                    raise ValueError(f"{race}: more than one Vermont statewide entry")
                statewide = tally(entry, race)
                continue
            place = town(name)
            counts = tally(entry, f"{race} {place}")
            if place in towns:
                if set(towns[place]) != set(counts):
                    raise ValueError(f"{race} {place}: district entries list other candidates")
                counts = {k: (n, p, v + towns[place][k][2]) for k, (n, p, v) in counts.items()}
            towns[place] = counts
        if statewide is None or not towns:
            raise ValueError(f"{race}: Vermont office lacks its statewide or town counts")
        for key, (name, party, votes) in statewide.items():
            if sum(t[key][2] for t in towns.values() if key in t) != votes or any(
                key not in t or t[key][:2] != (name, party) for t in towns.values()
            ):
                raise ValueError(f"{race}: towns do not add up to the statewide count")
            rows.append(result_row(race, identity, key, name, party, votes))
        for place, counts in towns.items():
            if set(counts) != set(statewide):
                raise ValueError(f"{race} {place}: town lists other candidates")
            for key, (name, party, votes) in counts.items():
                rows.append(result_row(race, identity, key, name, party, votes, county=place))
        verify_scope("VT", race, [k for k in statewide if k != WRITE_IN[0]], sorted(towns))
    if {race for race in OFFICES.values()} != set(wanted):
        raise ValueError("Vermont results lack a selected contest")
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats.update(
        official_results=official,
        contest_total_reconciliation="passed",
    )
