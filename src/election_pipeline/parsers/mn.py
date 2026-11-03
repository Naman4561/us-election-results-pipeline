"""Minnesota's documented 16-field semicolon exports, pinned in one bundle."""

import csv
import io
import re
import zipfile
from collections import defaultdict

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

MEDIA_BASE = "https://electionresultsfiles.sos.mn.gov/20261103/"
MEDIA_FILES = {
    "senate.txt": MEDIA_BASE + "ussenate.txt",
    "house-county.txt": MEDIA_BASE + "USHouseCty.txt",
    "house.txt": MEDIA_BASE + "USHouse.txt",
    "counties.txt": MEDIA_BASE + "Cntytbl.txt",
}


def validate_source(source):
    if (
        source.state != "MN"
        or source.dataset != "election_results"
        or source.format != "bundle"
        or source.election_date != "2026-11-03"
        or source.primary_filename != "senate-county.txt"
        or source.url != MEDIA_BASE + "ussenatecty.txt"
        or dict(source.files) != MEDIA_FILES
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
    validate_source(source)
    rows, expected, actual = [], {}, defaultdict(lambda: [0, 0])
    with zipfile.ZipFile(archive_path) as archive:
        # New bundles contain only the official data files. Keep legacy index validation
        # when replaying an older bundle; never reinterpret a CAPTCHA as election evidence.
        if "election.html" in archive.namelist():
            index = archive.read("election.html").decode("utf-8-sig")
            if "radware captcha page" in index.lower():
                raise ValueError(
                    "Minnesota legacy index contains a CAPTCHA; collect a fresh media bundle"
                )
            if "Tuesday, November 3, 2026" not in index:
                raise ValueError("Minnesota index belongs to another election")
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
                if raw[4] == "U.S. Representative District 1" and raw[5] == "1":
                    race = "MN-01"
                    if raw[3] != "0104":
                        raise ValueError("Unexpected Minnesota House office ID")
                if race is None:
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
        if expected != dict(actual):
            raise ValueError("Minnesota county coverage/candidate precinct counts disagree")
        for candidate in {r["source_candidate_id"] for r in rows if r["contest_id"] == "MN-SEN"}:
            if {
                r["county"]
                for r in rows
                if r["unit_type"] == "county"
                and r["contest_id"] == "MN-SEN"
                and r["source_candidate_id"] == candidate
            } != {v[0] for v in counties.values()}:
                raise ValueError("Minnesota Senate candidate missing county rows")
        # Each geographic row repeats its contest denominator. Check it independently of aggregates.
        denominators, sums = {}, defaultdict(int)
        for name in (source.primary_filename, "senate.txt", "house-county.txt", "house.txt"):
            for raw in flat(archive, name, 16, settings.max_csv_rows):
                if raw[3] not in {"0102", "0104"}:
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
