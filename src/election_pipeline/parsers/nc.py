"""North Carolina file contracts. Candidate votes and ballot demographics stay separate."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

from election_pipeline.parsers.common import integer, records
from election_pipeline.settings import Source

# The official file's contest names for every NC race this pipeline covers.
CONTESTS = {
    "US SENATE": "NC-SEN",
    **{f"US HOUSE OF REPRESENTATIVES DISTRICT {n:02d}": f"NC-{n:02d}" for n in range(1, 15)},
}
# The official file's vote-method columns, in order; they add up to Total Votes.
METHODS = ("election_day", "early_voting", "absentee_by_mail", "provisional")

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
DEMO_STATE = (
    "election_date",
    "state",
    "request_type",
    "party_registration",
    "sex",
    "race",
    "ethnicity",
)


def unique(seen, key, line):
    if key in seen:
        raise ValueError(f"Line {line}: duplicate record key {key!r}")

    seen.add(key)


def result_rows(path: Path, source: Source, limit: int, stats: dict):
    from election_pipeline.states import state_adapter

    if set(CONTESTS.values()) != state_adapter("NC").races.keys():
        raise ValueError("NC contest names differ from the registered races")
    if len(source.expected_members) != 1:
        raise ValueError("NC results require exactly one configured ZIP member")

    seen, counties, contests = set(), set(), set()
    parties = {}

    stats.update(
        input_rows=0,
        output_rows=0,
        administrative_rows_retained=0,
        nul_party_values_normalized=0,
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
                contest = CONTESTS.get(row["Contest Name"])

                if contest is None:
                    continue

                # The official NC file uses a single NUL for a missing party on some write-ins.
                # Normalize that one field only; the original ZIP remains unchanged.
                if row["Choice Party"] == "\x00":
                    row["Choice Party"] = ""
                    stats["nul_party_values_normalized"] += 1
                bad_fields = [name for name in RESULT_HEADER if "\x00" in row[name]]
                if bad_fields:
                    raise ValueError(f"Line {line}: unexpected NUL in {', '.join(bad_fields)}")

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
                    unit_type="precinct",
                    county=county,
                    precinct=row["Precinct"],
                    contest_id=contest,
                    source_contest_id=row["Contest Group ID"],
                    source_candidate_id=row["Choice"],
                    choice=row["Choice"],
                    choice_party=party,
                    total_votes=votes[4],
                    precincts_reporting=None,
                    precincts_total=None,
                    **dict(zip(METHODS, votes[:4])),
                    attributes={
                        "contest_name": row["Contest Name"],
                        "contest_type": row["Contest Type"],
                        # "N" marks administrative rows: absentee, one-stop, provisional.
                        "real_precinct": row["Real Precinct"] == "Y",
                    },
                )

    missing = set(CONTESTS.values()) - contests

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
    from election_pipeline.parsers.results import finish, write_groups

    if source.state != "NC":
        raise ValueError("No parser registered for this state")
    if source.dataset == "election_results" and source.format == "zip":
        rows = list(result_rows(archive, source, settings.max_csv_rows, stats))
        finish(rows, source, settings, output, stats, reference=reference)
        if stats["nul_party_values_normalized"]:
            stats["warnings"].append(
                f"Normalized {stats['nul_party_values_normalized']} NUL-only party values "
                "to missing."
            )
        stats["warnings"].append("Candidate votes are not unique ballots or demographic turnout.")
        return
    if source.dataset != "absentee_early_demographics" or source.format != "csv":
        raise ValueError("No parser registered for this NC dataset/format")
    if reference is not None:
        raise ValueError("Candidate-vote reference comparisons apply only to election results")
    statewide, total = {}, 0
    with (output / "county_demographics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DEMO_FIELDS)
        writer.writeheader()
        for row in demographic_rows(archive, source, settings.max_csv_rows, stats):
            writer.writerow(row)
            total += row["ballot_count"]
            key = tuple(row[name] for name in DEMO_STATE)
            statewide[key] = statewide.get(key, 0) + row["ballot_count"]
    write_groups(
        output / "state_demographics.csv",
        {k: [v] for k, v in statewide.items()},
        DEMO_STATE,
        ("ballot_count",),
    )
    if sum(statewide.values()) != total:
        raise ValueError("Aggregation did not preserve input counts")
    stats.update(
        internal_reconciliation="passed",
        all_counts_zero=not total,
        reference_comparison={"status": "not_applicable"},
        warnings=["All counts are zero; this does not establish live reporting."]
        if not total
        else [],
        election_status="not determined by parser",
    )


# The absentee/early-voting demographics dataset keeps its own NC tables.
DEMOGRAPHIC_TYPES = {
    "source_line": "int",
    "county": "text",
    "request_type": "text",
    "party_registration": "optional",
    "sex": "optional",
    "race": "optional",
    "ethnicity": "optional",
    "ballot_count": "int",
}
DATASETS = {
    "absentee_early_demographics": {
        "county_demographics.csv": (
            ("nc", "absentee_demographics"),
            DEMOGRAPHIC_TYPES,
            "output_rows",
        )
    }
}
