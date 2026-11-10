"""Utah, Indiana, Missouri, Arkansas and North Dakota: recorded official responses and
adversarial edits.

The fixtures are official bytes captured by the production collector; see
``tests/fixtures/expansion/provenance.json``. Mutations happen in temporary copies.
"""

import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from election_pipeline.dashboard.server import STATIC, make_payload
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/expansion"
LEDGER = json.loads((FIXTURES / "provenance.json").read_text())
SOURCES = {"UT": "ut_2026_general"}
HOSTS = {"UT": "electionresults.utah.gov"}


def members(code):
    with zipfile.ZipFile(FIXTURES / f"{code.lower()}.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def edit_json(data, name, change):
    value = json.loads(data[name])
    change(value)
    data[name] = json.dumps(value).encode()


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")

    def parse(self, code, mutate=None):
        self.source = self.sources[SOURCES[code]]
        data = members(code)
        if mutate:
            mutate(data)
        raw = self.root / "input.zip"
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row["total_votes"] = int(row["total_votes"]) if row["total_votes"] else None
        return rows

    def failed(self, code, mutate, message=None):
        report = self.parse(code, mutate)
        self.assertEqual(report["outcome"], "validation_failed", report)
        if message:
            self.assertIn(message, json.dumps(report))
        return report

    def test_fixture_provenance_and_configuration_match(self):
        self.assertEqual(set(LEDGER), set(SOURCES))
        for code, entry in LEDGER.items():
            raw = (FIXTURES / f"{code.lower()}.zip").read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["archive_sha256"], code)
            for component in entry["components"]:
                self.assertEqual(component["url"].split("/")[2], HOSTS[code], code)

    def test_every_state_parses_and_draws_on_its_map(self):
        for code in SOURCES:
            with self.subTest(code=code):
                report = self.parse(code)
                self.assertEqual(report["outcome"], "parsed", report)
                geography = json.loads((STATIC / f"{code.lower()}-map.json").read_text())
                payload = make_payload(
                    geography,
                    {"status": "unofficial"},
                    self.read(report, "county_results.csv"),
                    self.read(report, "contest_results.csv"),
                    state=code,
                )
                self.assertEqual(
                    set(payload["races"]), {d["id"] for d in geography["districts"]}
                )

    # Utah ------------------------------------------------------------------------------

    def test_ut_counties_reconcile_and_the_published_totals_are_kept(self):
        report = self.parse("UT")
        self.assertEqual(report["contest_total_reconciliation"], "passed")
        self.assertIs(report["official_results"], False)
        totals = {
            (r["contest_id"], r["choice"]): r["total_votes"]
            for r in self.read(report, "contest_results.csv")
        }
        data = json.loads(members("UT")["data.json"])
        for item in data["ballotItems"]:
            name = item["name"][0]["text"]
            if name.startswith("U.S. House District "):
                race = f"UT-0{name[-1]}"
                for option in item["summaryResults"]["ballotOptions"]:
                    choice = " ".join(option["name"][0]["text"].split())
                    self.assertEqual(totals[(race, choice)], option["voteCount"], choice)

    def test_ut_wrong_election_altered_county_and_new_candidate_are_refused(self):
        def election(data):
            edit_json(data, "data.json", lambda v: v["election"].update(isProduction=False))

        def county(data):
            def change(value):
                option = value["breakdownResults"][0]["ballotOptions"][0]
                option["voteCount"] += 1

            edit_json(data, "item-ut-02.json", change)

        def stranger(data):
            def change(value):
                option = dict(value["breakdownResults"][0]["ballotOptions"][0])
                option["id"] = "00000000-0000-0000-0000-000000000000"
                value["breakdownResults"][0]["ballotOptions"].append(option)

            edit_json(data, "item-ut-01.json", change)

        self.failed("UT", election, "Wrong Utah election")
        self.failed("UT", county)
        self.failed("UT", stranger)


if __name__ == "__main__":
    unittest.main()
