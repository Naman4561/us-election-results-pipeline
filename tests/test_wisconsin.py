"""Wisconsin: each county's own results report, imported by hand as counties come in.

The fixture packs six county reports as posted (``tests/fixtures/wisconsin/
provenance.json``): Door's Election Summary Report, Chippewa's summary, Oneida's results
page, Portage's canvass statement, La Crosse's ES&S summary, and Iowa County's scanned
canvass with a transcribed review signed for the test only.
"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from election_pipeline.dashboard.server import STATIC, make_payload
from election_pipeline.manual import import_file
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/wisconsin"
DOWNLOADED = "2026-11-20T20:00:00+00:00"
STATEWIDE = {
    "WI-GOV": {
        "Tom Tiffany / David Varnam": 79621,
        "David Crowley / Sarah Godlewski": 91241,
        "Write-in": 207,
    },
    "WI-02": {"Mark Pocan": 8424, "Write-in": 441},
    "WI-03": {
        "Derrick Van Orden": 43499,
        "Rebecca Cooke": 63033,
        "Alexander Valiensi Kent": 1235,
        "Rustin Provance": 502,
        "Write-in": 26,
    },
    "WI-07": {"Michael Alfonso": 19340, "Fred Clark": 14084, "Write-in": 33},
    "WI-08": {"Tony Wied": 8198, "Rick Crosson": 9267, "Write-in": 12},
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


def edited(change_manifest=None, change_review=None, extra=None):
    data = members()
    manifest = json.loads(data["manifest.json"])
    review = json.loads(data["Iowa.review.json"])
    if change_manifest:
        change_manifest(manifest["counties"])
    if change_review:
        change_review(review)
    data["manifest.json"] = json.dumps(manifest).encode()
    data["Iowa.review.json"] = json.dumps(review).encode()
    data.update(extra or {})
    return packed(data)


class WisconsinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["wi_2026_general"]

    def imported(self, raw):
        path = self.root / "inbox" / "results.zip"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        receipt = import_file(
            self.source, self.settings, self.root, path, DOWNLOADED, hashlib.sha256(raw).hexdigest()
        )
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        return receipt, report

    def refused(self, raw, message):
        with self.assertRaisesRegex(ValueError, message):
            self.imported(raw)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_saved_reports(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        self.assertEqual(
            hashlib.sha256((FIXTURE / "results.zip").read_bytes()).hexdigest(), ledger["sha256"]
        )
        for name, raw in members().items():
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ledger["members"][name])

    def test_collected_counties_add_up_to_the_statewide_votes(self):
        receipt, report = self.imported((FIXTURE / "results.zip").read_bytes())
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(receipt["source_generated_at"], "2026-11-17T11:00:02-06:00")
        self.assertEqual(report["contests"], sorted(STATEWIDE))
        statewide = {}
        for found in self.read(report, "contest_results.csv"):
            statewide.setdefault(found["contest_id"], {})[found["choice"]] = int(
                found["total_votes"]
            )
        self.assertEqual(statewide, STATEWIDE)
        counties = {}
        for found in self.read(report, "county_results.csv"):
            counties.setdefault(found["contest_id"], set()).add(found["county"])
        self.assertEqual(
            counties["WI-GOV"], {"Door", "Chippewa", "Oneida", "Portage", "La Crosse", "Iowa"}
        )
        # Chippewa is in two districts and reports both.
        self.assertEqual(counties["WI-03"], {"Chippewa", "Portage", "La Crosse"})
        self.assertEqual(counties["WI-07"], {"Chippewa", "Oneida"})

    def test_a_release_holds_the_governor_and_the_districts_collected(self):
        wisconsin = STATES["WI"]
        self.assertTrue(wisconsin.accepts({"WI-GOV"}))
        self.assertTrue(wisconsin.accepts({"WI-GOV", "WI-03"}))
        self.assertFalse(wisconsin.accepts({"WI-03"}))
        self.assertFalse(wisconsin.accepts({"WI-GOV", "WI-09"}))
        self.assertFalse(STATES["KY"].accepts({"KY-SEN"}))
        verify_scope("WI", "WI-02", ["Mark Pocan"], ["Iowa"], collected=True)
        with self.assertRaisesRegex(ValueError, "county coverage"):
            verify_scope("WI", "WI-02", ["Mark Pocan"], ["Iowa", "Door"], collected=True)
        with self.assertRaisesRegex(ValueError, "county coverage"):
            verify_scope("WI", "WI-02", ["Mark Pocan"], ["Iowa"])

    def test_the_dashboard_shows_partial_county_coverage(self):
        _, report = self.imported((FIXTURE / "results.zip").read_bytes())

        def rows(name):
            return [dict(r, total_votes=int(r["total_votes"])) for r in self.read(report, name)]

        geography = json.loads((STATIC / "wi-map.json").read_text())
        payload = make_payload(
            geography,
            {"publication_id": "x", "status": "test"},
            rows("county_results.csv"),
            rows("contest_results.csv"),
            state="WI",
        )
        coverage = payload["county_collection"]
        self.assertEqual((coverage["with_results"], coverage["expected"]), (6, 72))
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["basis"], "collected_county_reports")
        self.assertEqual(payload["county_feeds"]["DANE"]["collection_status"], "not_collected")
        self.assertEqual(payload["county_feeds"]["IOWA"]["collection_status"], "current")
        # A district none of whose counties is in yet is served empty, all missing.
        self.assertEqual(payload["races"]["WI-04"]["missing_counties"], ["Milwaukee"])
        self.assertIsNone(payload["races"]["WI-04"]["stats"]["total_votes"]["leader"])
        self.assertEqual(
            payload["races"]["WI-02"]["missing_counties"],
            ["Dane", "Green", "Lafayette", "Rock", "Sauk"],
        )

    def test_a_review_must_be_signed_for_its_report_and_add_up(self):
        self.refused(
            edited(change_review=lambda r: r.update(reviewed_by="")), "unsigned or for another"
        )
        self.refused(
            edited(change_review=lambda r: r.update(file_sha256="0" * 64)),
            "unsigned or for another",
        )
        self.refused(
            edited(change_review=lambda r: r["races"]["WI-02"].update({"Mark Pocan": 8425})),
            "do not add up",
        )

    def test_a_county_reports_exactly_its_races_and_ballot(self):
        self.refused(edited(change_review=lambda r: r["races"].pop("WI-02")), "its races are")

        def without_pocan(review):
            del review["races"]["WI-02"]["Mark Pocan"]
            review["totals"]["WI-02"] = 441

        self.refused(edited(change_review=without_pocan), "ballot candidate missing")
        self.refused(
            edited(change_manifest=lambda m: m.update(Atlantis=dict(m["Door"]))), "no county"
        )

    def test_a_county_report_posted_in_parts_is_read_from_its_review(self):
        # As Vilas posts its Governor and its House races as separate reports.
        second = b"%PDF-1.4 the county's second report (test)"
        digest = hashlib.sha256(second).hexdigest()
        first = json.loads(members()["manifest.json"])["counties"]["Iowa"]["sha256"]
        parts = [{"file": "Iowa.pdf", "sha256": first}, {"file": "Iowa_2.pdf", "sha256": digest}]

        def split(manifest):
            for key in ("file", "sha256"):
                manifest["Iowa"].pop(key)
            manifest["Iowa"]["parts"] = parts

        def both(review):
            review["file_sha256"] = [first, digest]

        _, report = self.imported(
            edited(change_manifest=split, change_review=both, extra={"Iowa_2.pdf": second})
        )
        self.assertEqual(report["outcome"], "parsed")
        rows = self.read(report, "contest_results.csv")
        governor = {r["choice"]: int(r["total_votes"]) for r in rows if r["contest_id"] == "WI-GOV"}
        self.assertEqual(governor, STATEWIDE["WI-GOV"])

        # The review must name every part, in order.
        self.refused(
            edited(change_manifest=split, extra={"Iowa_2.pdf": second}), "unsigned or for another"
        )
        self.refused(
            edited(
                change_manifest=split,
                change_review=lambda r: r.update(file_sha256=[digest, first]),
                extra={"Iowa_2.pdf": second},
            ),
            "unsigned or for another",
        )
        # Parts are two or more files, listed in place of one, and only a review reads them.
        self.refused(
            edited(change_manifest=lambda m: m["Iowa"].update(parts=parts[:1])), "in parts"
        )
        self.refused(
            edited(
                change_manifest=lambda m: m["Iowa"].update(parts=parts),
                extra={"Iowa_2.pdf": second},
            ),
            "in parts",
        )

        def door_in_parts(manifest):
            door = manifest["Door"]
            door["parts"] = [{"file": door.pop("file"), "sha256": door.pop("sha256")}] + parts[1:]

        self.refused(
            edited(change_manifest=door_in_parts, extra={"Iowa_2.pdf": second}), "in parts"
        )
        # Every part is in the bundle with its listed SHA-256.
        self.refused(edited(change_manifest=split, change_review=both), "Iowa_2.pdf")
        self.refused(
            edited(change_manifest=split, change_review=both, extra={"Iowa_2.pdf": b"changed"}),
            "SHA-256",
        )

    def test_files_must_match_the_manifest(self):
        self.refused(edited(change_manifest=lambda m: m["Door"].update(sha256="0" * 64)), "SHA-256")
        self.refused(edited(extra={"notes.txt": b"x"}), "do not match its manifest")
        self.refused(
            edited(change_manifest=lambda m: m["Door"].update(format="spreadsheet")),
            "unknown report kind or format",
        )


if __name__ == "__main__":
    unittest.main()
