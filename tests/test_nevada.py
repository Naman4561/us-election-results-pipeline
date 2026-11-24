"""Nevada's results app data files, saved by hand and imported as one zip.

The fixture packs the files the operator saved on November 19, 2026, unchanged
(``tests/fixtures/nevada/provenance.json``).
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

from election_pipeline.dashboard.server import STATIC
from election_pipeline.manual import import_file
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/nevada"
DOWNLOADED = "2026-11-19T22:12:00+00:00"
GOVERNOR = "9c3f603616365ee909467b2bb295abda2ea28c1de2f20ea0cf51001d99128ac0.json"
HOUSE_2 = "d2973c4c2d29445299f29ec8da7fea119d50981a35d2665a776be617c157cfde.json"
EXPECTED = {
    "NV-GOV": {
        "Aaron D. Ford": 568433,
        "Joseph Lombardo": 482325,
        "Danielle Ford": 32028,
        "None of These Candidates": 16228,
    },
    "NV-02": {"Teresa F. Benitez-Thompson": 163773, "David Flippo": 143336, "Lynn Chapman": 17757},
}


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def packed(data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), value)
    return out.getvalue()


def resealed(data, name, change):
    """A member changed and re-hashed in the manifest, as a consistent forgery would be."""
    value = json.loads(data[name])
    change(value)
    raw = json.dumps(value).encode()
    sha = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(data["manifest.json"])
    old = name.removesuffix(".json")
    for contest in manifest["contests"]:
        if contest["results"]["sha256"] == old:
            contest["results"]["sha256"] = sha
    data = {k: v for k, v in data.items() if k != name} | {f"{sha}.json": raw}
    return data | {"manifest.json": json.dumps(manifest).encode()}


class NevadaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["nv_2026_general"]

    def imported(self, raw):
        path = self.root / "inbox" / "results.zip"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source, self.settings, self.root, path, DOWNLOADED, hashlib.sha256(raw).hexdigest()
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_packs_the_saved_files(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        digest = hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest()
        self.assertEqual(digest, ledger["sha256"])
        self.assertEqual(set(members()), set(ledger["members"]))

    def test_counties_add_up_to_the_statewide_totals_with_lincolns_mask(self):
        receipt, report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-13T23:36:19.406545+00:00")
        self.assertEqual(receipt["source_generated_basis"], "document")
        self.assertIs(report["official_results"], False)
        self.assertEqual(report["masked_counties"], {"NV-02": ["Lincoln"]})
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(set(statewide), {"NV-GOV", "NV-01", "NV-02", "NV-03", "NV-04"})
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        counties = {}
        for found in self.read(report, "county_results.csv"):
            counties.setdefault(found["contest_id"], set()).add(found["county"])
        self.assertEqual(len(counties["NV-GOV"]), 17)
        self.assertNotIn("Lincoln", counties["NV-02"])
        self.assertIn("Lincoln", counties["NV-04"])
        geo = json.loads((STATIC / "nv-map.json").read_text())
        keys = {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in counties["NV-GOV"]}
        self.assertEqual(keys, {f["id"] for f in geo["counties"]})

    def test_a_file_the_manifest_does_not_vouch_for_is_refused(self):
        data = members()
        data[GOVERNOR] = data[GOVERNOR].replace(b'"votes": 11160', b'"votes": 11161', 1)
        if data[GOVERNOR] == members()[GOVERNOR]:
            data[GOVERNOR] = data[GOVERNOR].replace(b'"votes":11160', b'"votes":11161', 1)
        self.assertNotEqual(data[GOVERNOR], members()[GOVERNOR])
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(packed(data))

    def test_consistent_but_wrong_counts_are_refused(self):
        def county(value):
            value["counties"][0]["choices"][0]["votes"] += 1

        def masked(value):
            for choice in value["counties"][0]["choices"]:
                choice["votes"] = None
            value["counties"][0]["lead"] = {
                "status": "Masked",
                "choiceIds": [],
                "marginVotes": None,
            }

        for name, change in (("county", county), ("masked", masked)):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(packed(resealed(members(), GOVERNOR, change)))
        with self.assertRaisesRegex(ValueError, "rejected"):
            data = members()
            data.pop(HOUSE_2)
            self.imported(packed(data))


if __name__ == "__main__":
    unittest.main()
