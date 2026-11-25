"""West Virginia and Arkansas: recorded official responses and adversarial edits.

Each fixture is a bundle captured by the production collector from the state's official
results site; ``tests/fixtures/two_states/provenance.json`` records every component URL
and hash. Adversarial cases edit temporary copies.
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
from election_pipeline.collect import FetchResponse
from election_pipeline.parsers import clarity, wv
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/two_states"
LEDGER = json.loads((FIXTURES / "provenance.json").read_text())
CODES = ("WV",)
HOSTS = {"WV": "results.enr.clarityelections.com"}


def members(code):
    with zipfile.ZipFile(FIXTURES / f"{code.lower()}.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def edit_detail(data, settings, change):
    raw = change(clarity.detail_member(data["detail.zip"], settings, "xml"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("detail.xml", raw)
    data["detail.zip"] = buffer.getvalue()


class TwoStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")

    def parse(self, code, mutate=None):
        self.source = self.sources[code.lower() + "_2026_general"]
        data = members(code)
        if mutate:
            mutate(data)
        raw = self.root / "input.zip"
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.store = ArchiveStore(self.root, self.source)
        target = self.store.archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            for field in state_adapter(self.source.state).methods:
                row[field] = int(row[field]) if row[field] else None
        return rows

    def failed(self, code, mutate, message=None):
        report = self.parse(code, mutate)
        self.assertEqual(report["outcome"], "validation_failed", report)
        if message:
            self.assertIn(message, json.dumps(report))
        return report

    # Shared contract -------------------------------------------------------------------

    def test_fixture_hosts_and_configuration_match(self):
        self.assertEqual(set(LEDGER), set(CODES))
        for code in CODES:
            # Each file's SHA-256 is checked in tests/test_fixtures.py.
            entry = LEDGER[code]
            data = members(code)
            source = self.sources[code.lower() + "_2026_general"]
            self.assertEqual(set(data), set(source.expected_members), code)
            for component in entry["components"]:
                self.assertEqual(component["url"].split("/")[2], HOSTS[code], code)
                self.assertIn(component["name"], data)
        # West Virginia elects no governor in 2026.
        self.assertNotIn("WV-GOV", state_adapter("WV").races)

    # West Virginia (Clarity) -----------------------------------------------------------

    def test_wv_published_figures(self):
        report = self.parse("WV")
        self.assertEqual(report["vote_methods"], [])
        senate = {
            r["choice"]: r["total_votes"]
            for r in self.read(report, "contest_results.csv")
            if r["contest_id"] == "WV-SEN"
        }
        self.assertEqual(
            senate,
            {
                "SHELLEY MOORE CAPITO": 266067,
                "RACHEL FETTY ANDERSON": 172922,
                "S. MARSHALL WILSON": 17139,
            },
        )
        self.assertEqual(
            wv.race("U.S. HOUSE OF REPRESENTATIVES, 2nd Congressional District"), "WV-02"
        )
        self.assertIsNone(wv.race("U.S. HOUSE OF REPRESENTATIVES, 3rd Congressional District"))
        self.assertIsNone(wv.race("STATE SENATOR, 1st Senatorial District"))

    def test_wv_county_edit_summary_mismatch_and_foreign_detail_rejected(self):
        def county(raw):
            return re.sub(
                rb'(<County name="Barbour" votes=")(\d+)',
                lambda m: m[1] + str(int(m[2]) + 1).encode(),
                raw,
                count=1,
            )

        self.failed("WV", lambda d: edit_detail(d, self.settings, county))
        self.failed(
            "WV",
            lambda d: d.update(
                {"summary.json": d["summary.json"].replace(b"266067", b"266068", 1)}
            ),
        )
        self.failed(
            "WV",
            lambda d: edit_detail(
                d,
                self.settings,
                lambda raw: raw.replace(b"<Region>WV</Region>", b"<Region>VA</Region>"),
            ),
            "belongs to another election",
        )

    def test_wv_split_vote_types_or_unreviewed_candidate_rejected(self):
        def split(raw):
            return raw.replace(b'VoteType name="Election Day"', b'VoteType name="Absentee"', 1)

        self.failed(
            "WV",
            lambda d: edit_detail(d, self.settings, split),
            "unexpected West Virginia vote types",
        )

        def candidate(raw):
            return raw.replace(
                b'key="005" text="S. MARSHALL WILSON"', b'key="006" text="S. MARSHALL WILSON"', 1
            )

        def summary(data):
            edit_detail(data, self.settings, candidate)

        self.failed("WV", summary, "candidate roster differs")
        for bad in ("", "1a", "-1", None):
            with self.assertRaises(ValueError):
                clarity.choice_key(bad, "West Virginia")
        self.assertEqual(clarity.choice_key("005", "West Virginia"), 5)

    def test_wv_settings_must_name_west_virginia_in_eastern_time(self):
        raw = members("WV")["settings.json"]
        self.assertTrue(clarity.settings_check(raw, "WV", self.settings).endswith(" EST"))
        with self.assertRaises(ValueError):
            clarity.settings_check(raw, "CO", self.settings)
        with self.assertRaises(ValueError):
            clarity.settings_check(raw.replace(b" EST", b" GMT"), "WV", self.settings)

    def test_wv_collector_reads_only_the_current_version(self):
        source = self.sources["wv_2026_general"]
        data = members("WV")
        version = data["current_ver.txt"].decode().strip()
        paths = {relative: name for name, relative in wv.VERSION_FILES.items()}

        def fetch(src, settings, path, headers, *, url=None, form=None, cookies=None):
            if url == source.url:
                body, kind = data["current_ver.txt"], "text/plain; charset=utf-8"
            else:
                self.assertIn(f"/127496/{version}/", url)
                name = paths[url.split(f"/{version}/", 1)[1]]
                body = data[name]
                kind = "application/zip" if name.endswith(".zip") else "application/json"
            path.write_bytes(body)
            return FetchResponse(
                200, {"content-type": kind}, url, len(body), hashlib.sha256(body).hexdigest()
            )

        destination = self.root / "wv.zip"
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            bundles.fetch_bundle(source, self.settings, destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual({n: archive.read(n) for n in archive.namelist()}, data)


if __name__ == "__main__":
    unittest.main()
