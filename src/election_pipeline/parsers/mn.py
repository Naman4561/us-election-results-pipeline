"""Minnesota's documented 16-field semicolon exports, pinned in one bundle."""

import csv
import io
import re
import zipfile
from collections import defaultdict

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.results import finish

MEDIA_BASE = "https://electionresultsfiles.sos.mn.gov/20261103/"
MEDIA_FILES = {
    "senate.txt": MEDIA_BASE + "ussenate.txt",
    "house-county.txt": MEDIA_BASE + "USHouseCty.txt",
    "house.txt": MEDIA_BASE + "USHouse.txt",
    "counties.txt": MEDIA_BASE + "Cntytbl.txt",
}
GOVERNOR_FILES = {
    "governor.txt": MEDIA_BASE + "Governor.txt",
    "governor-precinct.txt": MEDIA_BASE + "governorpct.txt",
}


def validate_source(source):
    if (
        source.state != "MN"
        or source.dataset != "election_results"
        or source.format != "bundle"
        or source.election_date != "2026-11-03"
        or source.primary_filename != "senate-county.txt"
        or source.url != MEDIA_BASE + "ussenatecty.txt"
        or dict(source.files) != {**MEDIA_FILES, **GOVERNOR_FILES}
    ):
        raise ValueError("Minnesota requires the verified 2026 election-dated media-file URLs")


def flat(archive, name, fields, limit):
    text = archive.read(name).decode("utf-8-sig")
    if re.search(r"<!doctype\s+html|<html\b", text[:4096], re.IGNORECASE):
        raise ValueError(f"Minnesota {name}: received HTML instead of a semicolon data file")
    reader = csv.reader(io.StringIO(text), delimiter=";", strict=True)
    for index, values in enumerate(reader, 1):
        if len(values) != fields or index > limit:
            raise ValueError(f"Minnesota {name}: unexpected field count/row limit")
        yield [v.strip() for v in values]


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    validate_source(source)
    wanted = state_adapter("MN").races
    rows, expected, actual = [], {}, defaultdict(lambda: [0, 0])
    with zipfile.ZipFile(archive_path) as archive:
        counties = {}
        for identity, name, precincts in flat(archive, "counties.txt", 3, settings.max_csv_rows):
            if identity in counties or not name:
                raise ValueError("Duplicate/blank county reference")
            counties[identity] = (name.upper(), integer(precincts, "county precincts"))
        if len(counties) != 87 or len({v[0] for v in counties.values()}) != 87:
            raise ValueError("Minnesota requires 87 distinct county references")
        for name, kind in (
            (source.primary_filename, "county"),
            ("senate.txt", "state"),
            ("house-county.txt", "county"),
            ("house.txt", "state"),
        ):
            for raw in flat(archive, name, 16, settings.max_csv_rows):
                if raw[0] != "MN" or raw[2]:
                    raise ValueError("Minnesota export has wrong state/geographic grain")
                race = "MN-SEN" if raw[3:6] == ["0102", "U.S. Senator", ""] else None
                match = re.fullmatch(r"U\.S\. Representative District ([1-8])", raw[4])
                if match:
                    district = int(match[1])
                    race = f"MN-{district:02}"
                    if raw[3] != f"{103 + district:04}" or raw[5] != str(district):
                        raise ValueError("Unexpected Minnesota House office ID")
                if race not in wanted:
                    continue
                if ("senate" in name) != (race == "MN-SEN"):
                    raise ValueError("Contest appears in wrong Minnesota file")
                county = ""
                if kind == "county":
                    if raw[1] not in counties:
                        raise ValueError("Unknown Minnesota county")
                    county = counties[raw[1]][0]
                elif raw[1]:
                    raise ValueError("Official race totals have a county ID")
                reporting, precincts, votes, office_total = [
                    integer(raw[n], "MN count") for n in (11, 12, 13, 15)
                ]
                if reporting > precincts or votes > office_total:
                    raise ValueError("Minnesota count exceeds its denominator")
                if not re.fullmatch(r"[0-9]{4}", raw[6]):
                    raise ValueError("Invalid Minnesota candidate order code")
                key = (race, raw[6], raw[7], raw[10])
                if kind == "state":
                    expected[key] = [reporting, precincts]
                else:
                    actual[key] = [a + b for a, b in zip(actual[key], [reporting, precincts])]
                    if race == "MN-SEN" and precincts != counties[raw[1]][1]:
                        raise ValueError("Minnesota county precinct denominator disagrees")
                rows.append(
                    dict(
                        unit_type=kind,
                        county=county,
                        contest_id=race,
                        source_contest_id=raw[3],
                        source_candidate_id=raw[6],
                        choice=raw[7],
                        choice_party=raw[10],
                        total_votes=votes,
                        precincts_reporting=reporting,
                        precincts_total=precincts,
                    )
                )
        if "MN-GOV" in wanted:
            governor_rows, governor_expected, governor_actual = governor(
                archive, counties, settings.max_csv_rows
            )
            rows.extend(governor_rows)
            expected.update(governor_expected)
            actual.update(governor_actual)
        if expected != dict(actual):
            raise ValueError("Minnesota county coverage/candidate precinct counts disagree")
        for race in ("MN-SEN", "MN-GOV"):
            for candidate in {r["source_candidate_id"] for r in rows if r["contest_id"] == race}:
                if {
                    r["county"]
                    for r in rows
                    if r["unit_type"] == "county"
                    and r["contest_id"] == race
                    and r["source_candidate_id"] == candidate
                } != {v[0] for v in counties.values()}:
                    raise ValueError("Minnesota statewide candidate missing county rows")
        # Each geographic row repeats its contest denominator. Check it independently of aggregates.
        denominators, sums = {}, defaultdict(int)
        for name in (source.primary_filename, "senate.txt", "house-county.txt", "house.txt"):
            for raw in flat(archive, name, 16, settings.max_csv_rows):
                if raw[3] not in {"0102", *(f"{103 + n:04}" for n in range(1, 9))}:
                    continue
                key = (name, raw[1], raw[3])
                value = integer(raw[15], "MN office votes")
                if key in denominators and denominators[key] != value:
                    raise ValueError("Inconsistent Minnesota office denominator")
                denominators[key] = value
                sums[key] += integer(raw[13], "MN candidate votes")
        if denominators != dict(sums):
            raise ValueError("Minnesota candidates do not sum to official office totals")
    finish(rows, source, settings, output, stats, reference=reference, reconcile=True)


def governor(archive, counties, limit):
    """Aggregate the officially linked precinct export to county totals."""
    rows, expected, actual, aggregates = [], {}, defaultdict(lambda: [0, 0]), {}
    sums, denominators, seen, geography = defaultdict(int), {}, set(), defaultdict(set)
    for name, kind in (("governor.txt", "state"), ("governor-precinct.txt", "precinct")):
        for raw in flat(archive, name, 16, limit):
            if (
                raw[0] != "MN"
                or raw[3:6] != ["0331", "Governor & Lt Governor", ""]
                or not re.fullmatch(r"[0-9]{4}", raw[6])
            ):
                raise ValueError("Unexpected Minnesota governor office/candidate identity")
            reporting, precincts, votes, office_total = [
                integer(raw[n], "MN governor count") for n in (11, 12, 13, 15)
            ]
            if reporting > precincts or votes > office_total:
                raise ValueError("Minnesota governor count exceeds its denominator")
            identity = (raw[1], raw[2], raw[6])
            if (kind, *identity) in seen:
                raise ValueError("Duplicate Minnesota governor candidate result")
            seen.add((kind, *identity))
            key = (name, raw[1], raw[2])
            if key in denominators and denominators[key] != office_total:
                raise ValueError("Inconsistent Minnesota governor office denominator")
            denominators[key] = office_total
            sums[key] += votes
            candidate = ("MN-GOV", raw[6], raw[7], raw[10])
            row = dict(
                unit_type="state",
                county="",
                contest_id="MN-GOV",
                source_contest_id="0331",
                source_candidate_id=raw[6],
                choice=raw[7],
                choice_party=raw[10],
                total_votes=votes,
                precincts_reporting=reporting,
                precincts_total=precincts,
            )
            if kind == "state":
                if raw[1] or raw[2]:
                    raise ValueError("Minnesota governor state total has a geographic ID")
                expected[candidate] = [reporting, precincts]
                rows.append(row)
            else:
                if (
                    raw[1] not in counties
                    or not re.fullmatch(r"[0-9]{4}", raw[2])
                    or precincts != 1
                ):
                    raise ValueError("Unknown Minnesota governor county/precinct grain")
                geography[(raw[1], candidate)].add(raw[2])
                target = aggregates.setdefault(
                    (raw[1], candidate),
                    {
                        **row,
                        "unit_type": "county",
                        "county": counties[raw[1]][0],
                        "total_votes": 0,
                        "precincts_reporting": 0,
                        "precincts_total": 0,
                    },
                )
                for field in ("total_votes", "precincts_reporting", "precincts_total"):
                    target[field] += row[field]
                actual[candidate] = [
                    a + b for a, b in zip(actual[candidate], [reporting, precincts])
                ]
    if denominators != dict(sums):
        raise ValueError("Minnesota governor candidates do not sum to official office totals")
    for county, (_, precincts) in counties.items():
        units = [geography[(county, candidate)] for candidate in expected]
        if not units or any(len(value) != precincts or value != units[0] for value in units):
            raise ValueError("Minnesota governor candidate precinct coverage is incomplete")
    rows.extend(aggregates.values())
    return rows, expected, dict(actual)
