"""Offline KY fixtures cover reporting levels, reconciliation and replay contracts."""

import csv
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree.ElementTree import Element, SubElement, tostring

from loading import copies

from election_pipeline import bundles
from election_pipeline.collect import FetchResponse
from election_pipeline.database import verify
from election_pipeline.parsing import parse_source
from election_pipeline.settings import CollectorSettings, load_config
from election_pipeline.storage import ArchiveStore


def xml_rows(root_name, rows):
    root = Element(root_name)
    for row in rows:
        item = SubElement(root, "Row")
        for key, value in row.items():
            SubElement(item, key).text = str(value)
    return tostring(root)


class KentuckyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _, sources = load_config(Path(__file__).resolve().parents[1] / "config/sources.toml")
        self.source = sources["ky_2026_general"]
        self.settings = CollectorSettings()
        self.store = ArchiveStore(self.root, self.source)
        self.units = [{"UnitId": 1, "UnitType": "State", "UnitName": "Kentucky", "ParentId": 0}]
        geography = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "src/election_pipeline/dashboard/static/ky-map.json"
            ).read_text()
        )
        self.units += [
            {"UnitId": i + 10, "UnitType": "County", "UnitName": f["name"], "ParentId": 1}
            for i, f in enumerate(geography["counties"])
        ]
        self.contests = [
            {"ContestId": i + 1, "ContestName": name}
            for i, name in enumerate(
                ["UNITED STATES SENATOR"]
                + [
                    f"UNITED STATES REPRESENTATIVE in CONGRESS {i}"
                    f"{['th', 'st', 'nd', 'rd', 'th', 'th', 'th'][i]} Congressional District"
                    for i in range(1, 7)
                ]
            )
        ]
        self.candidates = [
            {
                "CandidateId": i * 2 + j,
                "ContestId": i + 1,
                "BallotName": f"Candidate {j}",
                "PoliticalPartyId": j,
            }
            for i in range(7)
            for j in (1, 2)
        ]
        self.results = []
        for candidate in self.candidates:
            for unit in self.units:
                total = (120 if unit["UnitType"] == "State" else 1) * candidate["PoliticalPartyId"]
                self.results.append(
                    {
                        "CandidateId": candidate["CandidateId"],
                        "ContestId": candidate["ContestId"],
                        "ReportingUnitId": unit["UnitId"],
                        "ElectionDayVotes": total,
                        "TotalVotes": total * 2,
                    }
                )
        self.reports = [
            {
                "ReportingUnitId": u["UnitId"],
                "ReportingStatus": "PartialReporting",
                "PrecinctsParticipating": 120 if u["UnitType"] == "State" else 1,
                "PrecinctsReporting": 1,
                "BallotsCast": 20,
                "RegisteredVoters": 100,
            }
            for u in self.units
        ]

    def archive(self, mutate=None):
        election = Element("Election")
        for key, value in {
            "ElectionDate": "2026-11-03T00:00:00",
            "ElectionType": "General",
        }.items():
            SubElement(election, key).text = value
        result = Element("SummaryReport")
        for section, rows in [("CandidateData", self.results), ("ReportData", self.reports)]:
            result.append(Element(section))
            for row in rows:
                item = SubElement(result[-1], section)
                for key, value in row.items():
                    SubElement(item, key).text = str(value)
        members = {
            "results.xml": tostring(result),
            "election.xml": tostring(election),
            "units.xml": xml_rows("Units", self.units),
            "contests.xml": xml_rows("Contests", self.contests),
            "candidates.xml": xml_rows("Candidates", self.candidates),
            "parties.xml": xml_rows(
                "Parties",
                [{"PartyId": 1, "Abbreviation": "DEM"}, {"PartyId": 2, "Abbreviation": "REP"}],
            ),
        }
        if mutate:
            mutate(members)
        path = self.root / "fixture.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, value in members.items():
                z.writestr(name, value)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = self.store.archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        return digest

    def parse(self, mutate=None):
        digest = self.archive(mutate)
        return parse_source(self.source, self.settings, self.root, sha256=digest)

    def test_levels_preserved_and_only_counties_aggregated(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["output_rows"], 1694)
        directory = Path(report["output_dir"])
        with (directory / "contest_results.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 14)
        self.assertEqual({r["total_votes"] for r in rows}, {"240", "480"})
        self.assertNotIn("early_voting", rows[0])
        loaded = copies(self.source, self.settings, self.store, directory, 4)
        units = loaded[("landing", "reporting_units")]
        self.assertEqual(len(units), 121)
        self.assertEqual(units[0][:3], (4, 2, "KY"))
        self.assertEqual(units[0][5], "")  # State-level county remains empty text.
        methods = loaded[("landing", "result_methods")]
        self.assertEqual(len(methods), 2 * report["output_rows"])

    def test_state_county_disagreement_blocks_publication(self):
        self.results[0]["TotalVotes"] += 1
        report = self.parse()
        self.assertEqual(report["outcome"], "validation_failed")
        self.assertIn("disagree", report["error"])

    def test_duplicate_candidate_rows_rejected(self):
        self.results.append(self.results[1].copy())
        self.assertIn("Duplicate", self.parse()["error"])

    def test_unknown_candidate_and_missing_county_fail_closed(self):
        self.results[0]["CandidateId"] = 9999
        self.assertEqual(self.parse()["outcome"], "validation_failed")
        self.results = self.results[1:]
        self.assertEqual(self.parse()["outcome"], "validation_failed")

    def test_invalid_election_and_xml_entities_rejected(self):
        report = self.parse(
            lambda members: members.update({"election.xml": b"<!DOCTYPE x><Election/>"})
        )
        self.assertIn("forbidden", report["error"])
        report = self.parse(
            lambda members: members.update(
                {
                    "election.xml": b"<Election><ElectionDate>2024-11-05</ElectionDate>"
                    b"<ElectionType>General</ElectionType></Election>"
                }
            )
        )
        self.assertIn("different election", report["error"])

    def test_zero_counts_are_accepted_but_explicit(self):
        for row in self.results:
            row["ElectionDayVotes"] = row["TotalVotes"] = 0
        report = self.parse()
        self.assertTrue(report["all_counts_zero"])
        self.assertEqual(report["internal_reconciliation"], "passed")

    def test_bundle_is_replayable_and_deterministic(self):
        def fetch(source, settings, path, headers, *, url):
            path.write_bytes(url.encode())
            return FetchResponse(200, {}, url, len(url), hashlib.sha256(url.encode()).hexdigest())

        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            one = bundles.fetch_bundle(self.source, self.settings, self.root / "one.zip")
            two = bundles.fetch_bundle(self.source, self.settings, self.root / "two.zip")
        self.assertEqual(one.sha256, two.sha256)
        self.assertEqual(len(one.components), 6)
        with zipfile.ZipFile(self.root / "one.zip") as z:
            self.assertEqual(set(z.namelist()), set(self.source.expected_members))

    def test_changed_artifact_hash_is_rejected(self):
        report = self.parse()
        directory = Path(report["output_dir"])
        with (directory / "reporting_units.csv").open("a") as f:
            f.write("corruption")
        with self.assertRaises(ValueError):
            verify(self.source, self.settings, self.store, directory)
