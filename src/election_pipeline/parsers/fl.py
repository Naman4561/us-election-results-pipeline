"""Florida Division of Elections statewide results file (tab-delimited, one row per county).

``results.txt`` is the "Download Results" text file linked from Florida Election Watch.
It has a row per race, county and candidate, with each county's precinct counts. Florida
publishes no separate statewide row, so each statewide total here is the sum of the
official county rows (Florida Election Watch's statewide pages show the same sums).
Florida does not publish vote-method (ballot type) detail in this file. District 10 was
uncontested and is not on the ballot or in the file.

Write-in candidates appear with party ``WRI`` and are kept as candidates. Governor rows
list the governor in the first-name field and the lieutenant governor in the last-name
field, separated by ``/``; they are shown as "Governor / Lieutenant Governor".
"""

import csv
import io
import re
import zipfile
from collections import defaultdict

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.structured import row
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

HEADER = (
    "ElectionDate\tPartyCode\tPartyName\tRaceCode\tRaceName\tCountyCode\tCountyName\tJuris1num\t"
    "Juris2num\tPrecincts\tPrecinctsReporting\tCanNameLast\tCanNameFirst\tCanNameMiddle\tCanVotes"
).split("\t")


def contest(values):
    code, name, district = values["RaceCode"], values["RaceName"], values["Juris1num"]
    if code == "USS" and name == "United States Senator" and not district:
        return "FL-SEN"
    if code == "GOV" and name == "Governor and Lieutenant Governor" and not district:
        return "FL-GOV"
    if code == "USR":
        match = re.fullmatch(r"Representative in Congress, District ([1-9][0-9]?)", name)
        if not match or not re.fullmatch(r"[0-9]{3}", district) or int(district) != int(match[1]):
            raise ValueError("Florida congressional race and district disagree")
        return f"FL-{int(match[1]):02}"
    return None


def candidate(values):
    last, first, middle = (values[k] for k in ("CanNameLast", "CanNameFirst", "CanNameMiddle"))
    if not last:
        raise ValueError("Missing Florida candidate name")
    if middle == "/":
        if not first:
            raise ValueError("Incomplete Florida ticket")
        name = f"{first} / {last}"
    else:
        name = " ".join(part for part in (first, middle, last) if part)
    identity = "|".join((values["PartyCode"], last, first, middle))
    return identity, name


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03" or source.state != "FL":
        raise ValueError("Unsupported Florida source/election")
    wanted = state_adapter("FL").selected_races(source)
    with zipfile.ZipFile(archive_path) as archive:
        text = archive.read(source.primary_filename).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
    if next(reader, []) != HEADER:
        raise ValueError("Unexpected Florida results header")
    counties = defaultdict(dict)  # race -> county -> {candidate identity: votes}
    precincts = defaultdict(dict)  # race -> county -> (reporting, total)
    names, parties, codes, source_ids = defaultdict(dict), {}, {}, {}
    for line, values in enumerate(reader, 2):
        if line > settings.max_csv_rows + 1 or len(values) != len(HEADER):
            raise ValueError("Unexpected Florida field count/row limit")
        values = dict(zip(HEADER, (v.strip() for v in values)))
        if values["ElectionDate"] != "11/03/2026":
            raise ValueError("Florida file belongs to another election")
        race = contest(values)
        if race not in wanted:
            continue
        if values["Juris2num"]:
            raise ValueError("Unexpected Florida sub-jurisdiction")
        county = values["CountyName"].upper()
        if not county or codes.setdefault(county, values["CountyCode"]) != values["CountyCode"]:
            raise ValueError("Missing or inconsistent Florida county")
        identity, name = candidate(values)
        race_code = values["RaceCode"] + (f"-{values['Juris1num']}" if values["Juris1num"] else "")
        if source_ids.setdefault(race, race_code) != race_code:
            raise ValueError("Inconsistent Florida race code")
        reporting = integer(values["PrecinctsReporting"], "precincts reporting")
        total = integer(values["Precincts"], "precincts")
        if reporting > total:
            raise ValueError("Florida reporting precincts exceed total")
        if precincts[race].setdefault(county, (reporting, total)) != (reporting, total):
            raise ValueError("Florida county precinct counts differ between candidates")
        if identity in counties[race].get(county, {}):
            raise ValueError("Duplicate Florida candidate row")
        if (
            names[race].setdefault(identity, name) != name
            or parties.setdefault(identity, values["PartyCode"]) != values["PartyCode"]
        ):
            raise ValueError("Inconsistent Florida candidate identity")
        counties[race].setdefault(county, {})[identity] = integer(values["CanVotes"], "votes")
    if set(counties) != set(wanted):
        raise ValueError("Florida file lacks a selected race")
    rows = []
    for race in sorted(counties):
        candidates = set(names[race])
        for county, votes in counties[race].items():
            if set(votes) != candidates:
                raise ValueError(f"{race}: {county} lacks a candidate row")
        verify_scope("FL", race, sorted(candidates), list(counties[race]))
        reporting = sum(p[0] for p in precincts[race].values())
        total = sum(p[1] for p in precincts[race].values())
        for identity in sorted(candidates):
            rows.append(
                row(
                    race,
                    source_ids[race],
                    identity,
                    names[race][identity],
                    parties[identity],
                    sum(v[identity] for v in counties[race].values()),
                    reporting=reporting,
                    total=total,
                )
            )
        for county in sorted(counties[race]):
            reporting, total = precincts[race][county]
            for identity in sorted(candidates):
                rows.append(
                    row(
                        race,
                        source_ids[race],
                        identity,
                        names[race][identity],
                        parties[identity],
                        counties[race][county][identity],
                        county=county,
                        reporting=reporting,
                        total=total,
                    )
                )
    finish(rows, source, settings, output, stats, reference=reference)
    stats.update(
        statewide_totals="sum_of_official_county_rows",
        vote_methods="not_published",
    )
