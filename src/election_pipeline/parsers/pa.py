"""Pennsylvania Department of State: the county results export, imported by hand.

The results site (electionreturns.pa.gov) and its RSS feeds refuse automated clients, so
the site's county results export is downloaded in a browser and imported as a manual
source (docs/manual-results.md). The export, ``UnOfficial_<MMDDYYYYhhmmss><AM|PM>.txt``,
is comma-separated with every field quoted: a header and a row per county, office and
candidate. It has no statewide rows; statewide totals are the county sums. (The site's
RSS feed stopped updating on November 10; the export is current.)

Each row's Election Day, mail and provisional votes must add up to its votes; they are
stored as ``election_day``, ``mail`` and ``provisional``. The yes/no columns are for
ballot questions and must be zero here. The export lists no write-ins and prints no
report time: the operator declares the site's time at import (``--generated-at``).
"""

import csv
import io
import re
import zipfile

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.platforms import party_names

HEADER = [
    "Election Name", "County Name", "Office Name", "District Name", "Party Name",
    "Candidate Name", "Votes", "Yes Votes", "No Votes", "Election Day Votes",
    "ElectionDay Yes Votes", "Election Day No Votes", "Mail Votes", "Mail Yes Votes",
    "Mail No Votes", "Provisional Votes", "Provisional Yes Votes", "Provisional No Votes",
]  # fmt: skip
METHODS = {
    "Election Day Votes": "election_day",
    "Mail Votes": "mail",
    "Provisional Votes": "provisional",
}
QUESTION = (
    "Yes Votes", "No Votes", "ElectionDay Yes Votes", "Election Day No Votes",
    "Mail Yes Votes", "Mail No Votes", "Provisional Yes Votes", "Provisional No Votes",
)  # fmt: skip
HOUSE = re.compile(r"(\d+)(?:st|nd|rd|th) Congressional District")
# The parties on Pennsylvania's 2026 ballot for these offices; any other is refused.
PARTIES = party_names("PA")
COUNTIES = (
    "Adams", "Allegheny", "Armstrong", "Beaver", "Bedford", "Berks", "Blair", "Bradford",
    "Bucks", "Butler", "Cambria", "Cameron", "Carbon", "Centre", "Chester", "Clarion",
    "Clearfield", "Clinton", "Columbia", "Crawford", "Cumberland", "Dauphin", "Delaware",
    "Elk", "Erie", "Fayette", "Forest", "Franklin", "Fulton", "Greene", "Huntingdon",
    "Indiana", "Jefferson", "Juniata", "Lackawanna", "Lancaster", "Lawrence", "Lebanon",
    "Lehigh", "Luzerne", "Lycoming", "McKean", "Mercer", "Mifflin", "Monroe", "Montgomery",
    "Montour", "Northampton", "Northumberland", "Perry", "Philadelphia", "Pike", "Potter",
    "Schuylkill", "Snyder", "Somerset", "Sullivan", "Susquehanna", "Tioga", "Union",
    "Venango", "Warren", "Washington", "Wayne", "Westmoreland", "Wyoming", "York",
)  # fmt: skip
# The export spells counties in capitals, McKean as "McKEAN".
NAMES = {name.upper(): name for name in COUNTIES} | {"McKEAN": "McKean"}
del NAMES["MCKEAN"]


def number(value, label):
    if not re.fullmatch(r"0|[1-9]\d{0,2}(?:,\d{3})*", value or ""):
        raise ValueError(f"Pennsylvania results: invalid {label} {value!r}")
    return int(value.replace(",", ""))


def race_name(values):
    office, district = values["Office Name"], values["District Name"]
    if office == "Governor":
        if district != "Statewide":
            raise ValueError("Pennsylvania results: unexpected Governor row")
        return "PA-GOV"
    if office == "Representative in Congress":
        match = HOUSE.fullmatch(district)
        if not match:
            raise ValueError(f"Pennsylvania results: unexpected district {district!r}")
        return f"PA-{int(match[1]):02}"
    return None


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "PA":
        raise ValueError("Unsupported Pennsylvania source/election")
    with zipfile.ZipFile(archive_path) as archive:
        text = archive.read(source.primary_filename).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""))
    if next(reader, None) != HEADER:
        raise ValueError("Pennsylvania export is not the county results file")
    wanted = state_adapter("PA").races
    totals, identities, counties = {}, {}, {}
    for line in reader:
        if not line:
            continue
        if len(line) != len(HEADER):
            raise ValueError("Pennsylvania results: unexpected row width")
        values = dict(zip(HEADER, line))
        if values["Election Name"] != "2026 General Election":
            raise ValueError("Pennsylvania results belong to another election")
        race = race_name(values)
        if race is None:
            continue
        if race not in wanted:
            raise ValueError(f"Pennsylvania results: unexpected race {race}")
        county = NAMES.get(values["County Name"])
        if county is None:
            raise ValueError(f"Pennsylvania results: unknown county {values['County Name']!r}")
        if any(number(values[k], "ballot-question count") for k in QUESTION):
            raise ValueError(f"{race}: Pennsylvania candidate row with yes/no votes")
        votes = number(values["Votes"], "vote count")
        split = {method: number(values[k], "vote count") for k, method in METHODS.items()}
        if sum(split.values()) != votes:
            raise ValueError(f"{race} {county}: vote methods do not add up to the count")
        party = PARTIES.get(values["Party Name"])
        name = " ".join(values["Candidate Name"].split())
        if party is None or not name:
            raise ValueError(f"{race}: unexpected Pennsylvania candidate or party {name!r}")
        # A candidate keeps one party in every county.
        if identities.setdefault((race, name), party) != party:
            raise ValueError(f"{race}: Pennsylvania candidate {name!r} differs between counties")
        key = (race, county, name)
        if key in totals:
            raise ValueError(f"{race}: duplicate Pennsylvania row for {name!r} in {county}")
        counties.setdefault(race, set()).add(county)
        totals[key] = (votes, split)
    if set(counties) != set(wanted):
        raise ValueError("Pennsylvania results lack a selected office")
    if counties["PA-GOV"] != set(COUNTIES):
        raise ValueError("PA-GOV: Pennsylvania results lack a county")
    rows = []
    for (race, county, name), (votes, split) in sorted(totals.items()):
        party = identities[(race, name)]
        rows.append({**row(race, race, name, name, party, votes, county), **split})
    for race in wanted:
        verify_scope("PA", race, [n for r, n in identities if r == race], counties[race])
    finish(rows, source, settings, output, stats, reference=reference)
