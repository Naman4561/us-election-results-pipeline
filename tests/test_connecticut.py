"""Connecticut's results app files, as the collector captured them, and its collector.

The fixture is one version of the app's files captured on November 19, 2026
(``tests/fixtures/connecticut/provenance.json``).
"""

import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from election_pipeline import bundles
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors import connecticut
from election_pipeline.dashboard.server import STATIC
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/connecticut"
EXPECTED = {
    # Fusion: each candidate's party lines added up under its major party.
    "CT-GOV": {
        ("Ned Lamont and Susan Bysiewicz", "DEM"): 743529 + 29844,
        ("Ryan Fazio and Matt Corey", "REP"): 496972 + 27785,
        ("Write-in", ""): 28 + 18 + 18,
    },
    "CT-02": {
        ("Joe Courtney", "DEM"): 182195,
        ("George Patrick Austin", "REP"): 103602 + 6068,
        ("Write-in", ""): 31,
    },
    "CT-04": {
        ("Jim Himes", "DEM"): 157485,
        ("Michael Ted Goldstein", "REP"): 78059,
        ("Benjamin Wesley", "IND"): 4594,
    },
}


def members():
    with zipfile.ZipFile(FIXTURE / "bundle.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def edited(member, change):
    """A mutation of one JSON member."""

    def mutate(data):
        value = json.loads(data[member])
        change(value)
        data[member] = json.dumps(value).encode()

    return mutate


class ConnecticutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ct_2026_general"]

    def parse(self, mutate=None):
        data = members()
        if mutate:
            mutate(data)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.getvalue()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.getvalue())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_captured_bundle(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        digest = hashlib.sha256((FIXTURE / "bundle.zip").read_bytes()).hexdigest()
        self.assertEqual(digest, ledger["sha256"])

    def test_party_lines_add_up_by_candidate_and_towns_to_statewide(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["source_update_text"], "version 55530")
        self.assertIs(report["official_results"], False)
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            choice = (found["choice"], found["choice_party"])
            statewide.setdefault(found["contest_id"], {})[choice] = int(found["total_votes"])
        self.assertEqual(set(statewide), {"CT-GOV", *(f"CT-0{n}" for n in range(1, 6))})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        towns = self.read(report, "county_results.csv")
        governor = {r["county"] for r in towns if r["contest_id"] == "CT-GOV"}
        self.assertEqual(len(governor), 169)
        # Every town is drawn on the map under the same key.
        geo = json.loads((STATIC / "ct-map.json").read_text())
        keys = {re.sub(r"[^A-Z0-9]", "", t.upper()) for t in governor}
        self.assertEqual(keys, {f["id"] for f in geo["counties"]})
        self.assertEqual(len(geo["districts"]), 5)

    def test_changed_votes_parties_or_election_are_refused(self):
        def town_votes(value):
            value["1"]["19663"][0]["44186"]["V"] = str(
                int(value["1"]["19663"][0]["44186"]["V"]) + 1
            )

        def state_votes(value):
            value["19663"][0]["44186"]["V"] = "743530"

        changes = {
            "town": edited("towns.json", town_votes),
            "statewide": edited("state.json", state_votes),
            "party": edited("lookup.json", lambda v: v["candidateIds"]["44186"].update(P="16")),
            "election": edited("lookup.json", lambda v: v["election"].update(DT="11/05/2024")),
            "status": edited("status.json", lambda v: v["1"].update(TS="( *Partial Results )")),
            "count": edited("state.json", lambda v: v["19663"][0]["44186"].update(V="7x3529")),
        }
        for name, mutate in changes.items():
            with self.subTest(name):
                report = self.parse(mutate)
                self.assertEqual(report["outcome"], "validation_failed", report)

    def test_official_only_when_every_town_says_so(self):
        def official(value):
            for town in value.values():
                town["TS"] = "( *Official Results )"

        self.assertIs(self.parse(edited("status.json", official))["official_results"], True)


class ConnecticutCollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ct_2026_general"]
        self.data = members()
        self.versions = []

    def fetch(self, calls):
        by_name = {name: member for member, name in connecticut.FILES.items()}

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            self.assertIsNone(form)
            calls.append(url)
            if url == self.source.url:
                body = self.versions.pop(0) if self.versions else self.data["version.json"]
            else:
                self.assertTrue(url.startswith(self.source.url.rsplit("/", 1)[0] + "/55530/"))
                body = self.data[by_name[url.rsplit("/", 1)[1]]]
            path.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            return FetchResponse(200, {"content-type": "application/json"}, url, len(body), digest)

        return fetch

    def collect(self, name, calls, previous=None, **options):
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=self.fetch(calls)):
            return bundles.fetch_bundle(
                self.source, self.settings, self.root / name, previous=previous, **options
            )

    def test_the_version_and_its_files_make_the_bundle(self):
        calls = []
        result = self.collect("first.zip", calls)
        self.assertEqual(len(calls), 7)  # the version, five files, the version again
        with zipfile.ZipFile(self.root / "first.zip") as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, self.data)
        self.assertEqual({c["name"] for c in result.components} - {".recheck.json"}, set(self.data))

    def test_an_unchanged_version_reuses_the_bundle(self):
        calls = []
        first = self.collect("first.zip", calls)
        calls.clear()
        again = self.collect("again.zip", calls, previous=self.root / "first.zip")
        self.assertEqual(again.sha256, first.sha256)
        self.assertEqual(calls, [self.source.url])

    def test_a_version_change_during_collection_or_a_bad_version_fails(self):
        calls = []
        self.versions = [self.data["version.json"], b'{"Version":55531}']
        with self.assertRaisesRegex(FetchFailure, "changed during collection"):
            self.collect("x.zip", calls)
        self.versions = [b'{"Version":"../55530"}']
        with self.assertRaises(FetchFailure):
            self.collect("y.zip", calls)


if __name__ == "__main__":
    unittest.main()
