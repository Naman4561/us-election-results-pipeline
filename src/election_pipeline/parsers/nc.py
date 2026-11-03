"""North Carolina file contracts. Candidate votes and ballot demographics stay separate."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

from election_pipeline.parsers.common import integer, records
from election_pipeline.settings import Source

COVERAGE = {
    "US SENATE": "NC-SEN",
    **{f"US HOUSE OF REPRESENTATIVES DISTRICT {n:02d}": f"NC-{n:02d}" for n in (1, 3, 4, 11)},
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

                election = datetime.strptime(row["Election Date"], "%m/%d/%Y").date().isoformat()

                if election != source.election_date:
                    raise ValueError(f"Line {line}: unexpected election date {election}")

                integer(row["Contest Group ID"], f"Line {line}: contest group ID")
                vote_for = integer(row["Vote For"], f"Line {line}: vote for")

                if vote_for < 1 or row["Real Precinct"] not in {"Y", "N"}:
                    raise ValueError(f"Line {line}: invalid vote-for or real-precinct flag")

                votes = [integer(row[name], f"Line {line}: {name}") for name in RESULT_HEADER[9:14]]

                if sum(votes[:4]) != votes[4]:
                    raise ValueError(f"Line {line}: voting-method counts do not equal Total Votes")

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
                    raise ValueError(f"Line {line}: tracked federal contest must be vote-for-one")

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


def parse(archive, source, settings, output, stats, reference):
    from election_pipeline.parsing import (
        DEMO_STATE,
        RESULT_CONTEST,
        RESULT_COUNTY,
        accumulate,
        reference_check,
        write_groups,
    )

    results = source.dataset == "election_results" and source.format == "zip"
    demographics = source.dataset == "absentee_early_demographics" and source.format == "csv"

    if source.state != "NC" or not (results or demographics):
        raise ValueError("No parser registered for this state/dataset/format")

    if reference is not None and not results:
        raise ValueError("Candidate-vote reference comparisons apply only to election results")

    fields = RESULT_FIELDS if results else DEMO_FIELDS
    measures = VOTES if results else ("ballot_count",)
    iterator = result_rows if results else demographic_rows
    county, statewide = {}, {}
    raw_totals = [0] * len(measures)

    name = "precinct_results.csv" if results else "county_demographics.csv"

    with (output / name).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()

        for row in iterator(archive, source, settings.max_csv_rows, stats):
            writer.writerow(row)

            for index, measure in enumerate(measures):
                raw_totals[index] += row[measure]

            if results:
                accumulate(county, row, RESULT_COUNTY, measures)
            else:
                accumulate(statewide, row, DEMO_STATE, measures)

    if results:
        for key, values in county.items():
            row = dict(zip((*RESULT_COUNTY, *measures), (*key, *values)))
            accumulate(statewide, row, RESULT_CONTEST, measures)

        write_groups(output / "county_results.csv", county, RESULT_COUNTY, measures)
        write_groups(output / "contest_results.csv", statewide, RESULT_CONTEST, measures)
    else:
        write_groups(output / "state_demographics.csv", statewide, DEMO_STATE, measures)

    # Internal transformation checks, not independent official validation.
    for groups in (county, statewide) if results else (statewide,):
        aggregated = [
            sum(values[index] for values in groups.values()) for index in range(len(measures))
        ]

        if aggregated != raw_totals:
            raise ValueError("Aggregation did not preserve input counts")

    stats["internal_reconciliation"] = "passed"
    stats["all_counts_zero"] = not any(raw_totals)

    stats["reference_comparison"] = (
        reference_check(reference, source, statewide, settings.max_csv_rows)
        if reference is not None
        else {"status": "not_provided" if results else "not_applicable"}
    )

    stats["warnings"] = []

    if stats["all_counts_zero"]:
        stats["warnings"].append("All counts are zero; this does not establish live reporting.")

    if results:
        stats["warnings"].append("Candidate votes are not unique ballots or demographic turnout.")

    stats["election_status"] = "not determined by parser"


def load_contract(dataset):
    from election_pipeline.states import LoadContract

    if dataset == "election_results":
        return LoadContract(
            "results",
            "precinct_results.csv",
            RESULT_FIELDS,
            ("precinct_results.csv", "county_results.csv", "contest_results.csv"),
            "zip",
        )
    if dataset == "absentee_early_demographics":
        return LoadContract(
            "absentee_demographics",
            "county_demographics.csv",
            DEMO_FIELDS,
            ("county_demographics.csv", "state_demographics.csv"),
            "csv",
        )
    raise ValueError("Unsupported NC dataset")


def database_row(row, source, release):
    from election_pipeline.database import number

    prefix = (
        release,
        number(row["source_line"]),
        row["county"],
    )

    if source.dataset == "election_results":
        if row["real_precinct"] not in {"Y", "N"}:
            raise ValueError("Invalid real-precinct flag")

        return (
            *prefix,
            row["precinct"],
            row["contest_id"],
            row["contest_name"],
            row["source_contest_group_id"],
            row["contest_type"],
            row["choice"],
            row["choice_party"] or None,
            number(row["vote_for"]),
            row["real_precinct"] == "Y",
            *(number(row[name]) for name in VOTES),
        )

    else:
        return (
            *prefix,
            row["request_type"],
            *(
                row[name] or None
                for name in (
                    "party_registration",
                    "sex",
                    "race",
                    "ethnicity",
                )
            ),
            number(row["ballot_count"]),
        )
