"""New Jersey's 21 county clerks' results, assembled into one zip and imported by hand.

The fixture is the bundle ``scripts/nj_bundle.py`` built on November 19, 2026
(``tests/fixtures/new_jersey/provenance.json``); its ``sources.json`` names every
member's address and SHA-256.
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
from election_pipeline.parsers import nj
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/new_jersey"
DOWNLOADED = "2026-11-19T23:00:00+00:00"
SENATE = {
    "Cory Booker": 1873425,
    "Justin Murphy": 1038578,
    "Veronica Fernandez": 50184,
    "Joanne Kuniansky": 28484,
    "Write-in": 4740,
}


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def packed(data, relist=True):
    """A bundle of ``data``; with ``relist`` its listing is rewritten to match (as a
    consistent forgery would be), so only the parser's own checks can refuse it."""
    if relist:
        listing = {n: e for n, e in json.loads(data["sources.json"]).items() if n in data}
        for name in listing:
            listing[name]["sha256"] = hashlib.sha256(data[name]).hexdigest()
        data = data | {"sources.json": json.dumps(listing).encode()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr(name, value)
    return out.getvalue()


def summary_edit(data, county, change):
    """A Clarity county's summary with ``change`` applied to its contests."""
    name = f"clarity/{county}.summary.json"
    contests = json.loads(data[name])
    change(contests)
    return data | {name: json.dumps(contests).encode()}


class NewJerseyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["nj_2026_general"]

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

    def test_the_fixture_is_the_built_bundle(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        digest = hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest()
        self.assertEqual(digest, ledger["sha256"])

    def test_every_county_counts_and_statewide_is_their_sum(self):
        receipt, report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-19T13:06:59-05:00")
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide["NJ-SEN"], SENATE)
        self.assertEqual(len(statewide), 13)
        counties = {}
        for found in self.read(report, "county_results.csv"):
            counties.setdefault(found["contest_id"], set()).add(found["county"])
        self.assertEqual(len(counties["NJ-SEN"]), 21)
        self.assertEqual(
            counties["NJ-07"], {"Hunterdon", "Morris", "Somerset", "Sussex", "Union", "Warren"}
        )
        # Burlington lists each written-in name; they are one Write-in line.
        burlington = {
            r["choice"]
            for r in self.read(report, "county_results.csv")
            if r["county"] == "Burlington" and r["contest_id"] == "NJ-SEN"
        }
        self.assertEqual(burlington, set(SENATE))
        geo = json.loads((STATIC / "nj-map.json").read_text())
        keys = {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in counties["NJ-SEN"]}
        self.assertEqual(keys, {f["id"] for f in geo["counties"]})

    def test_every_county_format_refuses_numbers_that_do_not_add_up(self):
        data = members()

        def clarity_count(contests):
            contests[0]["V"][0] += 1

        def missing_candidate(contests):
            for contest in contests:
                if contest["C"] == "United States Senate":
                    contest["C"] = "U S Senate"

        salem = data["salem.html"].replace(b"['JUSTIN MURPHY', 1855", b"['JUSTIN MURPHY', 1856", 1)
        salem = salem.replace(b", 10730]", b", 10731]", 1)
        changes = {
            "clarity total": summary_edit(data, "atlantic", clarity_count),
            "clarity contest": summary_edit(data, "atlantic", missing_candidate),
            "salem precincts": data | {"salem.html": salem},
        }
        for name, changed in changes.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(packed(changed))
        # A member that differs from its listed SHA-256 is refused before any parse.
        tampered = data | {"bergen.pdf": data["bergen.pdf"] + b" "}
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(packed(tampered, relist=False))
        with self.assertRaisesRegex(ValueError, "rejected"):
            self.imported(packed({k: v for k, v in data.items() if k != "warren.pdf"}))

    def test_spellings_and_marks_meet_one_key(self):
        for spelling in ("REP Justin MURPHY", "Justin Murphy (R)", "NMC Veronica FERNANDEZ"):
            self.assertIn(nj.key(spelling), ("JUSTINMURPHY", "VERONICAFERNANDEZ"))
        self.assertEqual(nj.key("Thomas H. KEAN, JR."), nj.key("THOMAS H. KEAN JR."))
        self.assertNotEqual(nj.key("ROB MENENDEZ"), "MENENDEZ")


if __name__ == "__main__":
    unittest.main()
