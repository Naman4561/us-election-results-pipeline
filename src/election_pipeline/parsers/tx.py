"""Texas Civix results and replay support for archived legacy HTML."""

import re
import zipfile
from decimal import Decimal
from html.parser import HTMLParser

from election_pipeline.parsers.common import integer
from election_pipeline.parsers.totals import database_row, finish, load_contract  # noqa: F401


class ResultsHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.title, self.polling = [], "", ""
        self.capture, self.in_table, self.cell, self.row, self.cells = None, False, None, [], []
        self.all_text = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "div" and attrs.get("class") in {"election", "polling"}:
            self.capture = attrs["class"]
            if self.capture == "election":
                self.title, self.polling = "", ""
            else:
                self.polling = ""
        if tag == "table" and attrs.get("class") == "election":
            if self.in_table:
                raise ValueError("Nested result table")
            self.in_table, self.cells = True, []
        if self.in_table and tag == "tr":
            self.row = []
        if self.in_table and tag in {"td", "th"}:
            self.cell = ""

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_data(self, data):
        self.all_text.append(data)
        if self.capture == "election":
            self.title += data
        elif self.capture == "polling":
            self.polling += data
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "div":
            self.capture = None
        if self.in_table and tag in {"td", "th"}:
            if self.cell is None:
                raise ValueError("Malformed result table cell")
            self.row.append(" ".join(self.cell.split()))
            self.cell = None
        if self.in_table and tag == "tr":
            self.cells.append(self.row)
        if self.in_table and tag == "table":
            self.tables.append((" ".join(self.title.split()), self.polling.strip(), self.cells))
            self.in_table = False


def parse(archive_path, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    if source.format != "bundle" or source.election_date != "2026-11-03":
        raise ValueError("Unsupported Texas source/election")
    if source.primary_filename == "results.json":
        from election_pipeline.parsers.tx_civix import parse as parse_civix

        return parse_civix(archive_path, source, settings, output, stats, reference)
    parser = ResultsHTML()
    with zipfile.ZipFile(archive_path) as archive:
        parser.feed(archive.read(source.primary_filename).decode("utf-8-sig"))
    parser.close()
    text = " ".join(" ".join(parser.all_text).split())
    if (
        "Election Results - Tuesday, November 3, 2026" not in text
        or "2026 NOVEMBER GENERAL ELECTION" not in text
        or parser.in_table
    ):
        raise ValueError("Texas page is incomplete or belongs to another election")
    selected, rows = set(), []
    wanted = state_adapter("TX").selected_races(source)
    for title, polling, table in parser.tables:
        race = {"U. S. SENATOR": "TX-SEN", "GOVERNOR": "TX-GOV"}.get(title)
        match = re.fullmatch(r"U\. S\. REPRESENTATIVE DISTRICT ([0-9]+)", title)
        if match:
            race = f"TX-{int(match[1]):02}"
        if race not in wanted:
            continue
        if race in selected or not table or table[0] != ["Party", "Candidate", "Ballots Cast"]:
            raise ValueError("Duplicate/malformed selected Texas table")
        selected.add(race)
        match = re.fullmatch(r"Polling Locations Reporting: ([0-9]+(?:\.[0-9]+)?)%", polling)
        if not match or not 0 <= Decimal(match[1]) <= 100:
            raise ValueError("Invalid Texas polling-location percentage")
        for values in table[1:]:
            if len(values) != 3:
                raise ValueError("Unexpected Texas result row shape")
            party, choice, value = values
            if value and not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value):
                raise ValueError("Invalid Texas vote count")
            if not value and Decimal(match[1]) != 0:
                raise ValueError("Blank Texas counts with reporting locations require review")
            rows.append(
                dict(
                    unit_type="state",
                    county="",
                    contest_id=race,
                    source_contest_id=title,
                    source_candidate_id=choice,
                    choice=choice,
                    choice_party=party,
                    total_votes=integer(value.replace(",", ""), "TX votes") if value else None,
                    precincts_reporting=None,
                    precincts_total=None,
                )
            )
    finish(rows, source, settings, output, stats, reference=reference)
