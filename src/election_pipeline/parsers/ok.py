"""Oklahoma State Election Board: the county results export, imported by hand.

The results site (results.okelections.gov) refuses automated clients, so its export is
downloaded in a browser and imported as a manual source (docs/manual-results.md). The
export is a zip holding one CSV, ``20261103_CountyResults.csv``: a row per county, race
and candidate with absentee-by-mail, early and Election Day votes and the county's
precincts reporting. It has no statewide rows; statewide totals are the county sums.

The CSV prints no report time. The site builds the zip when it is requested and dates
its one member then, in UTC (an export downloaded at 01:58:18 UTC held a member dated
01:58:16), so that date is the report's generation time.
"""

import csv
import io
import re
import zipfile
from datetime import datetime, timezone

from election_pipeline.parsers import certified
from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row

MEMBER = "20261103_CountyResults.csv"
HEADER = [
    "elec_date", "county", "entity_description", "race_number", "race_description",
    "race_party", "tot_race_prec", "race_prec_reporting", "cand_number", "cand_name",
    "cand_party", "cand_absmail_votes", "cand_early_votes", "cand_elecday_votes",
    "cand_tot_votes", "race_county_owner",
]  # fmt: skip
OFFICES = {"FOR UNITED STATES SENATOR": "OK-SEN", "FOR GOVERNOR": "OK-GOV"}
HOUSE = re.compile(r"FOR UNITED STATES REPRESENTATIVE DISTRICT (\d\d)")
# The parties on Oklahoma's 2026 ballot for these offices; any other is refused.
PARTIES = {"REP", "DEM", "LIB", "IND"}
COUNTIES = (
    "Adair", "Alfalfa", "Atoka", "Beaver", "Beckham", "Blaine", "Bryan", "Caddo",
    "Canadian", "Carter", "Cherokee", "Choctaw", "Cimarron", "Cleveland", "Coal",
    "Comanche", "Cotton", "Craig", "Creek", "Custer", "Delaware", "Dewey", "Ellis",
    "Garfield", "Garvin", "Grady", "Grant", "Greer", "Harmon", "Harper", "Haskell",
    "Hughes", "Jackson", "Jefferson", "Johnston", "Kay", "Kingfisher", "Kiowa", "Latimer",
    "Le Flore", "Lincoln", "Logan", "Love", "Major", "Marshall", "Mayes", "McClain",
    "McCurtain", "McIntosh", "Murray", "Muskogee", "Noble", "Nowata", "Okfuskee",
    "Oklahoma", "Okmulgee", "Osage", "Ottawa", "Pawnee", "Payne", "Pittsburg", "Pontotoc",
    "Pottawatomie", "Pushmataha", "Roger Mills", "Rogers", "Seminole", "Sequoyah",
    "Stephens", "Texas", "Tillman", "Tulsa", "Wagoner", "Washington", "Washita", "Woods",
    "Woodward",
)  # fmt: skip
# The export spells counties in capitals, some without spaces ("LEFLORE").
NAMES = {re.sub(r"[^A-Z]", "", name.upper()): name for name in COUNTIES}
METHODS = (
    ("absentee_by_mail", "cand_absmail_votes"),
    ("early_voting", "cand_early_votes"),
    ("election_day", "cand_elecday_votes"),
)


def race_name(description):
    if description in OFFICES:
        return OFFICES[description]
    match = HOUSE.fullmatch(description)
    return f"OK-{match[1]}" if match else None


def number(value, label):
    if not re.fullmatch(r"0|[1-9]\d*", value or ""):
        raise ValueError(f"Oklahoma results: invalid {label} {value!r}")
    return int(value)


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.election_date != "2026-11-03" or source.state != "OK":
        raise ValueError("Unsupported Oklahoma source/election")
    with zipfile.ZipFile(archive_path) as archive:
        export = archive.read(source.primary_filename)
    with zipfile.ZipFile(io.BytesIO(export)) as results:
        members = results.infolist()
        if [m.filename for m in members] != [MEMBER]:
            raise ValueError("Oklahoma export must hold only the county results CSV")
        info = members[0]
        if info.flag_bits & 1 or info.file_size > settings.max_uncompressed_bytes:
            raise ValueError("Oklahoma county results are encrypted or too large")
        text = results.read(info).decode("utf-8-sig")
    generated = datetime(*info.date_time, tzinfo=timezone.utc)
    if not datetime(2026, 11, 3, tzinfo=timezone.utc) <= generated:
        raise ValueError("Oklahoma export is dated before election day")
    reader = csv.reader(io.StringIO(text, newline=""))
    if next(reader, None) != HEADER:
        raise ValueError("Oklahoma export is not the county results CSV")
    wanted = state_adapter("OK").races
    rows, candidates, counties = [], {}, {}
    for line in reader:
        # Rows end with an empty column past the header's last.
        if len(line) == len(HEADER) + 1 and line[-1] == "":
            line = line[:-1]
        if len(line) != len(HEADER):
            raise ValueError("Oklahoma results: unexpected row width")
        values = dict(zip(HEADER, line))
        if values["elec_date"] != "11/3/2026":
            raise ValueError("Oklahoma results belong to another election")
        race = race_name(values["race_description"])
        if race is None:
            continue
        if race not in wanted or values["race_party"]:
            raise ValueError(f"Oklahoma results: unexpected race {values['race_description']!r}")
        county = NAMES.get(re.sub(r"[^A-Z]", "", values["county"]))
        if county is None or values["entity_description"] != values["county"] + " COUNTY":
            raise ValueError(f"Oklahoma results: unknown county {values['county']!r}")
        reporting = number(values["race_prec_reporting"], "precincts reporting")
        total = number(values["tot_race_prec"], "precinct count")
        if reporting > total:
            raise ValueError("Oklahoma reporting exceeds total precincts")
        name, affiliation = values["cand_name"], values["cand_party"]
        if affiliation not in PARTIES:
            raise ValueError(f"{race}: unexpected Oklahoma party {affiliation!r}")
        identity = (values["race_number"], values["cand_number"])
        # A candidate keeps one ballot number, name and party in every county.
        if candidates.setdefault((race, name), identity + (affiliation,)) != (
            identity + (affiliation,)
        ):
            raise ValueError(f"{race}: Oklahoma candidate {name!r} differs between counties")
        result = row(
            race, values["race_number"], values["cand_number"], name, affiliation,
            number(values["cand_tot_votes"], "vote count"), county, reporting, total,
        )  # fmt: skip
        result.update({m: number(values[c], "vote count") for m, c in METHODS})
        rows.append(result)
        counties.setdefault(race, set()).add(county)
    if set(counties) != set(wanted):
        raise ValueError("Oklahoma results lack a selected office")
    for race in ("OK-SEN", "OK-GOV"):
        if counties[race] != set(COUNTIES):
            raise ValueError(f"{race}: Oklahoma results lack a county")
    for race in wanted:
        verify_scope("OK", race, [n for r, n in candidates if r == race], counties[race])
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        source_update_text=f"{MEMBER} dated {generated:%Y-%m-%d %H:%M:%S} UTC",
        source_generated_at=generated.isoformat(),
    )


# Oklahoma's certification (Nov 13, 2026) was imported under this name before the general
# certified-official-results.zip existed; its archive is read the same way.
CERTIFIED = {"certified-county-results.zip": certified.parse_official}
