"""scripts/il_bundle.py: Illinois imports built from the previous import and an additions
file, with reviews written and signed (fixtures: ``tests/fixtures/illinois``)."""

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/illinois"
spec = importlib.util.spec_from_file_location("il_bundle", ROOT / "scripts/il_bundle.py")
il_bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(il_bundle)


def members(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def manifest_of(path):
    return json.loads(members(path)["manifest.json"])["authorities"]


class IllinoisBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # The tool reports to the operator on stdout; the tests read its files instead.
        quiet = mock.patch("sys.stdout", new_callable=io.StringIO)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        (self.inbox / "cumberland.json").write_bytes((FIXTURE / "gbs_cumberland.json").read_bytes())
        with zipfile.ZipFile(FIXTURE / "platinum_henry.zip") as archive:
            for name in archive.namelist():
                (self.inbox / f"henry.{name}").write_bytes(archive.read(name))

    def pack(self, additions=None, *, base=FIXTURE / "results.zip", remove=(), name="out.zip"):
        add = None
        if additions is not None:
            add = self.inbox / f"{name}.json"
            add.write_text(json.dumps(additions), encoding="utf-8")
        out = self.root / name
        il_bundle.pack(argparse.Namespace(base=base, add=add, remove=list(remove), out=out))
        return out

    def parse(self, path):
        settings, sources = load_config(ROOT / "config/sources.toml")
        source = sources["il_2026_general"]
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr(source.primary_filename, path.read_bytes())
        sha = hashlib.sha256(outer.getvalue()).hexdigest()
        target = ArchiveStore(self.root / "data", source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(outer.getvalue())
        return parse_source(source, settings, self.root / "data", sha256=sha)

    def counties(self, report):
        path = Path(report["output_dir"]) / "county_results.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            return {row["county"] for row in csv.DictReader(handle)}

    def test_a_repacked_import_keeps_its_files_and_packs_the_same_each_time(self):
        first, second = self.pack(name="a.zip"), self.pack(name="b.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        original = members(FIXTURE / "results.zip")
        repacked = members(first)
        self.assertEqual(original.keys(), repacked.keys())
        for name in original.keys() - {"manifest.json"}:
            self.assertEqual(original[name], repacked[name])
        self.assertEqual(manifest_of(first), manifest_of(FIXTURE / "results.zip"))

    def test_added_authorities_are_listed_and_parse(self):
        out = self.pack(
            {
                "Cumberland": {
                    "format": "gbs",
                    "report": "unofficial",
                    "url": "https://cumberland.results.gbsvote.com/results/cumberland/2026-general/current/results.json",
                    "file": "cumberland.json",
                    "saved_at": "2026-11-23T05:26:09+00:00",
                },
                "Henry": {
                    "format": "platinum",
                    "report": "unofficial",
                    "host": "platinumelectionresults.com",
                    "id": 76,
                    "pages": [f"henry.races{n}.html" for n in (1, 2, 3)],
                },
            }
        )
        manifest = manifest_of(out)
        self.assertEqual(len(manifest), 8)
        part = manifest["Cumberland"]["files"][0]
        self.assertEqual(part["file"], "gbs/Cumberland.json")
        self.assertEqual(
            part["sha256"],
            hashlib.sha256((FIXTURE / "gbs_cumberland.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            [p["url"] for p in manifest["Henry"]["files"]],
            [f"https://platinumelectionresults.com/races/76/{n}" for n in (1, 2, 3)],
        )
        report = self.parse(out)
        self.assertEqual(report["outcome"], "parsed", report.get("error"))
        self.assertTrue({"Cumberland", "Henry", "Lake"} <= self.counties(report))

    def test_a_removed_or_replaced_authority_leaves_none_of_its_files(self):
        out = self.pack(remove=["Lake"])
        self.assertNotIn("Lake", manifest_of(out))
        self.assertFalse([n for n in members(out) if "Lake" in n])
        report = self.parse(out)
        self.assertEqual(report["outcome"], "parsed", report.get("error"))
        self.assertNotIn("Lake", self.counties(report))
        with self.assertRaises(SystemExit):
            self.pack(remove=["Cook"], name="c.zip")

    def test_a_review_is_written_checked_and_packed_only_once_signed(self):
        (self.inbox / "adams.pdf").write_bytes(b"%PDF-1.7 Adams County canvass")
        transcription = self.inbox / "Adams.spec.json"
        transcription.write_text(
            json.dumps(
                {
                    "authority": "Adams",
                    "report": "canvassed",
                    "transcribed_by": "Claude",
                    "races": {"IL-SEN": {"Juliana Stratton": 10, "Don Tracy": 20}},
                    "totals": {"IL-SEN": 30},
                }
            ),
            encoding="utf-8",
        )
        reviews = self.inbox / "reviews"

        def write():
            il_bundle.review(
                argparse.Namespace(
                    spec=transcription, files=[self.inbox / "adams.pdf"], out=reviews
                )
            )

        write()
        entry = {
            "format": "review",
            "report": "canvassed",
            "url": "https://www.adamscountyil.gov/",
            "files": ["adams.pdf"],
            "review": "reviews/Adams.review.json",
        }
        with self.assertRaises(SystemExit):
            self.pack({"Adams": entry}, name="unsigned.zip")
        signing = argparse.Namespace(
            folder=reviews, signer="Naman", date="2026-11-24", authorities=["Adams"]
        )
        il_bundle.sign(signing)
        with self.assertRaises(SystemExit):
            il_bundle.sign(signing)
        with self.assertRaises(SystemExit):
            write()
        out = self.pack({"Adams": entry}, name="signed.zip")
        packed = members(out)
        self.assertEqual(packed["Adams.pdf"], b"%PDF-1.7 Adams County canvass")
        signed = json.loads(packed["Adams.review.json"])
        self.assertEqual((signed["schema"], signed["signed_by"]), ("election-review/1", "Naman"))
        (self.inbox / "adams.pdf").write_bytes(b"%PDF-1.7 another file")
        with self.assertRaises(SystemExit):
            self.pack({"Adams": entry}, name="other.zip")

    def test_a_transcription_that_disagrees_with_the_report_is_refused(self):
        (self.inbox / "adams.pdf").write_bytes(b"%PDF-1.7")
        transcription = self.inbox / "Adams.spec.json"
        transcription.write_text(
            json.dumps(
                {
                    "authority": "Adams",
                    "report": "canvassed",
                    "transcribed_by": "Claude",
                    "races": {"IL-SEN": {"Juliana Stratton": 10, "Don Tracy": 20}},
                    "totals": {"IL-SEN": 31},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit):
            il_bundle.review(
                argparse.Namespace(
                    spec=transcription, files=[self.inbox / "adams.pdf"], out=self.inbox
                )
            )
        self.assertFalse((self.inbox / "Adams.review.json").exists())


if __name__ == "__main__":
    unittest.main()
