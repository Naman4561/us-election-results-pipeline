"""North Carolina file contracts. Candidate votes and ballot demographics stay separate."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

from election_pipeline.settings import Source

COVERAGE = {
    "US SENATE": "NC-SEN",
    **{
        f"US HOUSE OF REPRESENTATIVES DISTRICT {n:02d}": f"NC-{n:02d}"
        for n in (1, 3, 4, 11)
    },
}

VOTES = (
    "election_day",
    "early_voting",
    "absentee_by_mail",
    "provisional",
    "total_votes",
)

RESULT_HEADER = (
    "County",
    "Election Date",
    "Precinct",
    "Contest Group ID",
    "Contest Type",
    "Contest Name",
    "Choice",
    "Choice Party",
    "Vote For",
    "Election Day",
    "Early Voting",
    "Absentee by Mail",
    "Provisional",
    "Total Votes",
    "Real Precinct",
)

DEMO_HEADER = (
    "county_name",
    "abs_request_type",
    "party_cd",
    "gender",
    "race",
    "ethnicity",
    "0",
)

RESULT_FIELDS = (
    "election_date",
    "state",
    "county",
    "precinct",
    "contest_id",
    "contest_name",
    "source_contest_group_id",
    "contest_type",
    "choice",
    "choice_party",
    "vote_for",
    "real_precinct",
    *VOTES,
    "source_line",
)

DEMO_FIELDS = (
    "election_date",
    "state",
    "county",
    "request_type",
    "party_registration",
    "sex",
    "race",
    "ethnicity",
    "ballot_count",
    "source_line",
)


def records(handle, required, delimiter, limit):
    """Read by column name; reject malformed shape."""
    reader = csv.reader(handle, delimiter=delimiter, strict=True)
    header = [name.strip() for name in next(reader, [])]

    if not header or any(not name for name in header) or len(set(header)) != len(header):
        raise ValueError("Missing, blank, or duplicate column names")

    if not set(required) <= set(header):
        raise ValueError(f"Missing columns: {sorted(set(required) - set(header))}")

    count = 0

    for values in reader:
        if not values:
            continue

        count += 1

        if count > limit or len(values) != len(header):
            raise ValueError(f"Line {reader.line_num}: row limit or field-count violation")

        yield reader.line_num, dict(zip(header, (value.strip() for value in values)))

    if not count:
        raise ValueError("File contains no data records")


def integer(value, label):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{label}: expected a nonnegative integer, got {value!r}")

    return int(value)


def unique(seen, key, line):
    if key in seen:
        raise ValueError(f"Line {line}: duplicate record key {key!r}")

    seen.add(key)


def result_rows(path: Path, source: Source, limit: int, stats: dict):
    if len(source.expected_members) != 1:
        raise ValueError("NC results require exactly one configured ZIP member")

    seen, counties, contests = set(), set(), set()
    parties = {}

    stats.update(
        input_rows=0,
        output_rows=0,
        administrative_rows_retained=0,
    )

    with zipfile.ZipFile(path) as archive:
        with io.TextIOWrapper(
            archive.open(source.expected_members[0]),
            encoding="utf-8-sig",
        ) as handle:
            for line, row in records(handle, RESULT_HEADER, "\t", limit):
                stats["input_rows"] += 1

                for name in ("County", "Precinct", "Contest Name", "Choice", "Contest Type"):
                    if not row[name]:
                        raise ValueError(f"Line {line}: empty {name}")

                election = datetime.strptime(
                    row["Election Date"], "%m/%d/%Y"
                ).date().isoformat()

                if election != source.election_date:
                    raise ValueError(f"Line {line}: unexpected election date {election}")

                integer(row["Contest Group ID"], f"Line {line}: contest group ID")
                vote_for = integer(row["Vote For"], f"Line {line}: vote for")

                if vote_for < 1 or row["Real Precinct"] not in {"Y", "N"}:
                    raise ValueError(f"Line {line}: invalid vote-for or real-precinct flag")

                votes = [
                    integer(row[name], f"Line {line}: {name}")
                    for name in RESULT_HEADER[9:14]
                ]

                if sum(votes[:4]) != votes[4]:
                    raise ValueError(
                        f"Line {line}: voting-method counts do not equal Total Votes"
                    )

                county = row["County"].upper()

                unique(
                    seen,
                    (county, row["Precinct"], row["Contest Name"], row["Choice"]),
                    line,
                )

                counties.add(county)
                contest = COVERAGE.get(row["Contest Name"])

                if contest is None:
                    continue

                if vote_for != 1:
                    raise ValueError(
                        f"Line {line}: tracked federal contest must be vote-for-one"
                    )

                choice_key = (contest, row["Choice"])
                party = row["Choice Party"]

                if parties.setdefault(choice_key, party) != party:
                    raise ValueError(f"Line {line}: inconsistent party for {choice_key}")

                contests.add(contest)
                stats["output_rows"] += 1
                stats["administrative_rows_retained"] += row["Real Precinct"] == "N"

                yield dict(
                    zip(
                        RESULT_FIELDS,
                        (
                            election,
                            source.state,
                            county,
                            row["Precinct"],
                            contest,
                            row["Contest Name"],
                            row["Contest Group ID"],
                            row["Contest Type"],
                            row["Choice"],
                            party,
                            vote_for,
                            row["Real Precinct"],
                            *votes,
                            line,
                        ),
                    )
                )

    missing = set(COVERAGE.values()) - contests

    if missing:
        raise ValueError(f"Missing tracked contests: {sorted(missing)}")

    stats.update(counties_seen=len(counties), contests=sorted(contests))


def demographic_rows(path: Path, source: Source, limit: int, stats: dict):
    seen, counties, kinds = set(), set(), set()
    stats.update(input_rows=0, output_rows=0)

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in records(handle, DEMO_HEADER, ",", limit):
            stats["input_rows"] += 1

            if not row["county_name"] or not row["abs_request_type"]:
                raise ValueError(f"Line {line}: missing county or request type")

            county = row["county_name"].upper()
            dimensions = (county, *(row[name] for name in DEMO_HEADER[1:6]))

            unique(seen, dimensions, line)

            count = integer(row["0"], f"Line {line}: ballot count")
            counties.add(county)
            kinds.add(row["abs_request_type"])
            stats["output_rows"] += 1

            yield dict(
                zip(
                    DEMO_FIELDS,
                    (
                        source.election_date,
                        source.state,
                        *dimensions,
                        count,
                        line,
                    ),
                )
            )

    stats.update(counties_seen=len(counties), request_types=sorted(kinds))
    stats["date_basis"] = "configured source; the CSV contains no election-date column"