"""Michigan Secretary of State: the county results export, imported by hand.

The results site (mvic.sos.state.mi.us) refuses automated clients, so its export is
downloaded in a browser and imported as a manual source (docs/manual-results.md). The
export, ``2026STATE_GENERAL_MI_CENR_BY_COUNTY.txt``, is tab-separated: a turnout line, a
header, a row per county, office and candidate, and a ``RECORDS: N`` line counting them.
It has no statewide rows; statewide totals are the county sums. The last line also
carries the state's label: ``RESULTS: OFFICIAL`` once the Board of State Canvassers has
certified (Nov 23, 2026), nothing after it before then.

Each office's write-in candidates (``WriteIn`` "Y") are kept as one "Write-in" line per
county. The export prints no report time: the site shows it ("Updated: ...") beside the
download link, and the operator declares it at import (``--generated-at``).
"""

import csv
import io
import re
import zipfile

from election_pipeline import parse_report
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.platforms import party_names

HEADER = [
    "ElectionDate", "OfficeCode(text)", "DistrictCode(Text)", "StatusCode", "CountyCode",
    "CountyName", "OfficeDescription", "PartyOrder", "PartyDescription", "CandidateID",
    "CandidateLastName", "CandidateFirstName", "CandidateMiddleName",
    "CandidateFormerName", "CandidateVotes", "WriteIn(W)/Uncommitted(Z)", "Recount(*)",
    "Nomindated(N)/Elected(E)",
]  # fmt: skip
OFFICES = {
    "7": ("MI-SEN", "UNITED STATES SENATOR 6 YEAR TERM (1) POSITION"),
    "3": ("MI-GOV", "GOVERNOR AND LIEUTENANT GOVERNOR 4 YEAR TERM (1) POSITION"),
}
HOUSE_OFFICE = "8"
HOUSE = re.compile(
    r"\d+(?:ST|ND|RD|TH) DISTRICT REPRESENTATIVE IN CONGRESS 2 YEAR TERM \(1\) POSITION"
)
# The parties on Michigan's 2026 ballot for these offices; any other is refused.
PARTIES = party_names("MI")
WRITE_IN = ("write-in", "Write-in")
# Michigan's county codes, in the export's alphabetical order (1 Alcona ... 83 Wexford).
COUNTIES = (
    "Alcona", "Alger", "Allegan", "Alpena", "Antrim", "Arenac", "Baraga", "Barry", "Bay",
    "Benzie", "Berrien", "Branch", "Calhoun", "Cass", "Charlevoix", "Cheboygan",
    "Chippewa", "Clare", "Clinton", "Crawford", "Delta", "Dickinson", "Eaton", "Emmet",
    "Genesee", "Gladwin", "Gogebic", "Grand Traverse", "Gratiot", "Hillsdale", "Houghton",
    "Huron", "Ingham", "Ionia", "Iosco", "Iron", "Isabella", "Jackson", "Kalamazoo",
    "Kalkaska", "Kent", "Keweenaw", "Lake", "Lapeer", "Leelanau", "Lenawee", "Livingston",
    "Luce", "Mackinac", "Macomb", "Manistee", "Marquette", "Mason", "Mecosta",
    "Menominee", "Midland", "Missaukee", "Monroe", "Montcalm", "Montmorency", "Muskegon",
    "Newaygo", "Oakland", "Oceana", "Ogemaw", "Ontonagon", "Osceola", "Oscoda", "Otsego",
    "Ottawa", "Presque Isle", "Roscommon", "Saginaw", "St. Clair", "St. Joseph",
    "Sanilac", "Schoolcraft", "Shiawassee", "Tuscola", "Van Buren", "Washtenaw", "Wayne",
    "Wexford",
)  # fmt: skip
# The export spells counties in capitals without periods ("ST CLAIR").
NAMES = {re.sub(r"[^A-Z]", "", name.upper()): name for name in COUNTIES}


def number(value, label):
    if not re.fullmatch(r"0|[1-9]\d*", value or ""):
        raise ValueError(f"Michigan results: invalid {label} {value!r}")
    return int(value)


def race_name(values):
    office, district = values["OfficeCode(text)"], values["DistrictCode(Text)"]
    if office in OFFICES:
        race, description = OFFICES[office]
        if district != "0" or values["OfficeDescription"] != description:
            raise ValueError(f"Michigan results: unexpected {race} office row")
        return race
    if office == HOUSE_OFFICE:
        if not re.fullmatch(r"\d\d", district) or not HOUSE.fullmatch(values["OfficeDescription"]):
            raise ValueError("Michigan results: unexpected U.S. House office row")
        return f"MI-{district}"
    return None


def candidate_name(values):
    parts = (
        values["CandidateFirstName"],
        values["CandidateMiddleName"],
        values["CandidateLastName"],
    )
    return " ".join(part.strip() for part in parts if part.strip())


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "MI":
        raise ValueError("Unsupported Michigan source/election")
    with zipfile.ZipFile(archive_path) as archive:
        text = archive.read(source.primary_filename).decode("utf-8-sig")
    lines = text.splitlines()
    if len(lines) < 4 or not re.fullmatch(r"TOTAL VOTER TURNOUT: [\d,]+", lines[0]):
        raise ValueError("Michigan export lacks its turnout line")
    footer = re.fullmatch(r"RECORDS: (\d+)\tRESULTS: (OFFICIAL)?", lines[-1])
    if not footer:
        raise ValueError("Michigan export lacks its record count")
    body = lines[1:-1]
    if int(footer[1]) != len(body) - 1:
        raise ValueError("Michigan export's record count differs from its rows")
    reader = csv.reader(io.StringIO("\n".join(body), newline=""), delimiter="\t")
    if next(reader, None) != HEADER:
        raise ValueError("Michigan export is not the county results file")
    wanted = state_adapter("MI").races
    totals, write_ins, identities, counties = {}, {}, {}, {}
    for line in reader:
        if len(line) != len(HEADER):
            raise ValueError("Michigan results: unexpected row width")
        values = dict(zip(HEADER, line))
        if values["ElectionDate"] != "2026-11-03":
            raise ValueError("Michigan results belong to another election")
        race = race_name(values)
        if race is None:
            continue
        if race not in wanted or values["StatusCode"] != "0":
            raise ValueError(f"Michigan results: unexpected race {values['OfficeDescription']!r}")
        county = NAMES.get(re.sub(r"[^A-Z]", "", values["CountyName"]))
        if county is None or number(values["CountyCode"], "county code") != (
            COUNTIES.index(county) + 1
        ):
            raise ValueError(f"Michigan results: unknown county {values['CountyName']!r}")
        if values["Recount(*)"] or values["CandidateFormerName"]:
            raise ValueError(f"{race}: unexpected Michigan recount or former-name mark")
        votes = number(values["CandidateVotes"], "vote count")
        counties.setdefault(race, set()).add(county)
        flag = values["WriteIn(W)/Uncommitted(Z)"]
        if flag == "Y":
            write_ins[(race, county)] = write_ins.get((race, county), 0) + votes
            continue
        if flag:
            raise ValueError(f"{race}: unexpected Michigan write-in/uncommitted mark {flag!r}")
        party = PARTIES.get(values["PartyDescription"])
        if party is None:
            raise ValueError(f"{race}: unexpected Michigan party {values['PartyDescription']!r}")
        name, candidate = candidate_name(values), values["CandidateID"]
        if not re.fullmatch(r"\d+", candidate) or not name:
            raise ValueError(f"{race}: Michigan candidate without an ID or name")
        # A candidate keeps one ID and party in every county.
        if identities.setdefault((race, name), (candidate, party)) != (candidate, party):
            raise ValueError(f"{race}: Michigan candidate {name!r} differs between counties")
        key = (race, county, name)
        if key in totals:
            raise ValueError(f"{race}: duplicate Michigan row for {name!r} in {county}")
        totals[key] = votes
    if set(counties) != set(wanted):
        raise ValueError("Michigan results lack a selected office")
    for race in ("MI-SEN", "MI-GOV"):
        if counties[race] != set(COUNTIES):
            raise ValueError(f"{race}: Michigan results lack a county")
    rows = []
    for (race, county, name), votes in sorted(totals.items()):
        candidate, party = identities[(race, name)]
        rows.append(row(race, race, candidate, name, party, votes, county))
    for (race, county), votes in sorted(write_ins.items()):
        rows.append(row(race, race, WRITE_IN[0], WRITE_IN[1], "", votes, county))
    for race in wanted:
        verify_scope("MI", race, [n for r, n in identities if r == race], counties[race])
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        # The state's own label: its canvassed export ends "RESULTS: OFFICIAL", the
        # election-week one "RESULTS: " with nothing after it.
        **parse_report.fields(footer[2] == "OFFICIAL"),
        total_voter_turnout=int(lines[0].split(": ", 1)[1].replace(",", "")),
        write_in_votes={
            race: sum(v for (r, _), v in write_ins.items() if r == race) for race in wanted
        },
    )
