"""Kentucky's results pages, saved by hand and imported as one zip.

The fixture packs the seven pages the operator saved on November 20, 2026, unchanged
(``tests/fixtures/kentucky_pages/provenance.json``).
"""

import csv
import email
import email.policy
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
from election_pipeline.parsers import ky_pages
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import ELECTION

ROOT = Path(__file__).resolve().parents[1]
PAGE = ELECTION.platforms["KY"]["page"]
FIXTURE = ROOT / "tests/fixtures/kentucky_pages"
DOWNLOADED = "2026-11-20T15:54:00+00:00"
EXPECTED = {
    "KY-SEN": {
        "Andy Barr": 784869,
        "Charles Booker": 612357,
        "Christopher Campbell": 29892,
        "Write-in": 158,
    },
    "KY-03": {"Morgan McGarvey": 174781, "Maria Teresa Rodriguez": 73719, "Write-in": 0},
    "KY-06": {
        "Zach Dembo": 138777,
        "Ralph Alvarado": 114030,
        "Jay J Bowman": 6710,
        "Pete Lynch": 3836,
        "Write-in": 208,
    },
}


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def packed(data):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, value in data.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED  # A ZipInfo is otherwise stored.
            archive.writestr(info, value)
    return out.getvalue()


def edited(raw, change):
    """A saved page with ``change`` applied to its Statewide page's HTML."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    for part in message.walk():
        if part.get_content_type() == "text/html" and part["Content-Location"] == PAGE:
            part.set_content(change(part.get_content()), subtype="html", cte="quoted-printable")
            part["Content-Location"] = PAGE
    return message.as_bytes()


class KentuckyPagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config")
        self.source = sources["ky_2026_general"]

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

    def test_the_fixture_is_the_saved_pages(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(
            hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest(), ledger["sha256"]
        )
        for name, raw in members().items():
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ledger["members"][name])

    def test_every_race_by_county_adds_up_to_its_statewide_totals(self):
        receipt, report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-20T10:42:18-05:00")
        self.assertEqual(report["county_reconciliation"], "passed")
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(len(statewide), 7)
        for race, expected in EXPECTED.items():
            self.assertEqual(statewide[race], expected)
        self.assertNotIn("election_day", self.read(report, "contest_results.csv")[0])
        counties = {}
        for found in self.read(report, "county_results.csv"):
            counties.setdefault(found["contest_id"], set()).add(found["county"])
        self.assertEqual(len(counties["KY-SEN"]), 120)
        self.assertEqual(counties["KY-03"], {"Jefferson"})
        geo = json.loads((STATIC / "ky-map.json").read_text())
        keys = {re.sub(r"[^A-Z0-9]", "", c.upper()) for c in counties["KY-SEN"]}
        self.assertEqual(keys, {f["id"] for f in geo["counties"]})

    def test_pages_that_do_not_add_up_or_match_are_refused(self):
        data = members()

        def county_vote(html):
            # Adair's Senate line for Andy Barr, in the county view only.
            return html.replace("4,724 - 79% of reported votes", "4,725 - 79% of reported votes", 1)

        def other_update(html):
            return html.replace("11/20/2026 10:42:18 AM", "11/20/2026 11:42:18 AM")

        def closed_view(html):
            return html.replace('id="myModal"', 'id="closed"')

        changes = {
            "county sum": data | {"KY-SEN.mhtml": edited(data["KY-SEN.mhtml"], county_vote)},
            "update": data | {"KY-02.mhtml": edited(data["KY-02.mhtml"], other_update)},
            "no county view": data | {"KY-04.mhtml": edited(data["KY-04.mhtml"], closed_view)},
            "wrong race": data | {"KY-01.mhtml": data["KY-02.mhtml"]},
            "missing page": {k: v for k, v in data.items() if k != "KY-05.mhtml"},
        }
        for name, changed in changes.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(packed(changed))

    def test_names_are_written_in_normal_case(self):
        self.assertEqual(ky_pages.display("Morgan McGARVEY"), "Morgan McGarvey")
        self.assertEqual(ky_pages.display("John &quot;Drew&quot; WILLIAMS"), 'John "Drew" Williams')
        self.assertEqual(ky_pages.display("S. Brett GUTHRIE"), "S. Brett Guthrie")


if __name__ == "__main__":
    unittest.main()
