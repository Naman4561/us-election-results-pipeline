"""Nebraska statewide media CSV; explicitly normalize its empty trailing field."""

import csv
import io
import re
import zipfile

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401

HEADER = (
    "RaceID,RaceName,PartyCode,AreaType,AreaNum,OfficeSeqNo,BallotOrder,CandidateID,"
    "CandidateName,CurrentDateTime,VoteFor,CandidateVotes,CandidatePercentage,PrecinctsReporting"
).split(",")
RACES = {
    "For United States Senator - 6 Year Term": "NE-SEN",
    "For Governor and Lt. Governor": "NE-GOV",
}


def parse(archive_path, source, settings, output, stats, reference=None):
    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported Nebraska source/election")
    rows, races = [], {}
    with zipfile.ZipFile(archive_path) as archive:
        index = archive.read("election.html").decode("utf-8-sig")
        if "General Election November 3, 2026" not in re.sub(r"<[^>]*>", "", index):
            raise ValueError("Nebraska index belongs to another election")
        reader = csv.reader(
            io.StringIO(archive.read(source.primary_filename).decode("utf-8-sig")), strict=True
        )
        if next(reader, []) != HEADER:
            raise ValueError("Unexpected Nebraska CSV header")
        for line, values in enumerate(reader, 2):
            if len(values) == len(HEADER) + 1 and values[-1] == "":
                values.pop()
            if len(values) != len(HEADER) or line > settings.max_csv_rows + 1:
                raise ValueError("Unexpected Nebraska field count/row limit")
            raw = dict(zip(HEADER, (v.strip() for v in values)))
            race = RACES.get(" ".join(raw["RaceName"].split()))
            if not race:
                continue
            if raw["AreaType"] != "SW" or raw["AreaNum"] or raw["VoteFor"] != "1":
                raise ValueError("Nebraska result is not a single-choice statewide total")
            if race in races and races[race] != raw["RaceID"]:
                raise ValueError("Duplicate Nebraska contest identity")
            races[race] = raw["RaceID"]
            integer(raw["RaceID"], "race ID")
            integer(raw["CandidateID"], "candidate ID")
            match = re.fullmatch(r'="([0-9]+)/([0-9]+)"', raw["PrecinctsReporting"])
            if not match:
                raise ValueError("Unexpected Nebraska precinct expression")
            reporting, precincts = map(int, match.groups())
            if reporting > precincts:
                raise ValueError("Nebraska reporting precincts exceed total")
            rows.append(
                dict(
                    unit_type="state",
                    county="",
                    contest_id=race,
                    source_contest_id=raw["RaceID"],
                    source_candidate_id=raw["CandidateID"],
                    choice=" ".join(raw["CandidateName"].split()),
                    choice_party=raw["PartyCode"],
                    total_votes=integer(raw["CandidateVotes"], "NE votes"),
                    precincts_reporting=reporting,
                    precincts_total=precincts,
                )
            )
    finish(rows, source, settings, output, stats, reference=reference)
