"""Multi-file manual imports are packed from the previous import by one tool."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_pipeline import packing
from election_pipeline.collect import FetchResponse
from election_pipeline.states import state_adapter

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
IMPORTS = {
    "IL": "illinois",
    "NJ": "new_jersey",
    "ME": "maine",
    "NH": "new_hampshire",
    "NV": "nevada",
    "WI": "wisconsin",
    "KY": "kentucky_pages",
}


def fixture(code):
    return (FIXTURES / IMPORTS[code] / "results.zip").read_bytes()


def layout(code):
    return state_adapter(code).parser.PACK


class PackingTests(unittest.TestCase):
    def test_every_multi_file_import_matches_its_declared_layout(self):
        for code in IMPORTS:
            with self.subTest(code=code):
                packing.check(layout(code), packing.read_zip(fixture(code)))

    def test_the_writer_reproduces_the_imports_it_packed_byte_for_byte(self):
        # Illinois's and Wisconsin's imports were written this way; the others were
        # packed by other means before this tool, so only their contents are checked.
        with tempfile.TemporaryDirectory() as directory:
            for code in ("IL", "WI"):
                out = Path(directory) / f"{code}.zip"
                packing.write_zip(out, packing.read_zip(fixture(code)))
                self.assertEqual(out.read_bytes(), fixture(code), code)

    def test_a_manifest_entry_is_added_replaced_and_removed_with_its_files(self):
        base = packing.read_zip(fixture("WI"))
        counties = json.loads(base["manifest.json"])["counties"]
        county, entry = sorted(counties.items())[0]
        report = entry.get("file") or entry["parts"][0]["file"]
        # Replacing an entry recomputes its files' hashes from the bytes.
        replaced = packing.pack(
            layout("WI"),
            base,
            files={report: base[report] + b" "},
            entries={county: {**entry, "sha256": "0" * 64}},
        )
        listed = json.loads(replaced["manifest.json"])["counties"][county]
        self.assertEqual(listed["sha256"], packing.sha256(base[report] + b" "))
        # Removing it removes its files and review.
        removed = packing.pack(layout("WI"), base, remove=[county])
        self.assertNotIn(county, json.loads(removed["manifest.json"])["counties"])
        self.assertNotIn(report, removed)
        self.assertNotIn(f"{county}.review.json", removed)
        with self.assertRaisesRegex(ValueError, "not in the previous import"):
            packing.pack(layout("WI"), base, remove=["Nowhere"])
        # A new entry may not take another entry's file, nor name a file not given.
        other = sorted(counties)[1]
        with self.assertRaisesRegex(ValueError, "belong to another entry"):
            packing.pack(layout("WI"), base, entries={other: {**entry}})
        with self.assertRaisesRegex(ValueError, "No file for member"):
            packing.pack(layout("WI"), base, entries={"New": {**entry, "file": "new.pdf"}})

    def test_a_tampered_or_unlisted_member_and_a_wrong_fixed_set_are_refused(self):
        members = packing.read_zip(fixture("NJ"))
        name = next(n for n in members if n != "sources.json")
        with self.assertRaisesRegex(ValueError, "differs from its listed hash"):
            packing.check(layout("NJ"), {**members, name: members[name] + b"x"})
        with self.assertRaisesRegex(ValueError, "exactly the import's files"):
            packing.check(layout("NJ"), {**members, "extra.pdf": b"x"})
        wisconsin = packing.read_zip(fixture("WI"))
        with self.assertRaisesRegex(ValueError, "Files nothing names"):
            packing.check(layout("WI"), {**wisconsin, "stray.pdf": b"x"})
        hampshire = packing.read_zip(fixture("NH"))
        with self.assertRaisesRegex(ValueError, "must be exactly"):
            packing.pack(layout("NH"), hampshire, remove=[sorted(hampshire)[0]])
        # New Jersey's listing records a replaced file's new hash.
        updated = packing.pack(
            layout("NJ"),
            members,
            files={name: members[name] + b" "},
            entries={name: json.loads(members["sources.json"])[name]},
        )
        self.assertEqual(
            json.loads(updated["sources.json"])[name]["sha256"],
            packing.sha256(members[name] + b" "),
        )

    def test_the_same_inputs_pack_to_the_same_bytes(self):
        base = packing.read_zip(fixture("ME"))
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "1.zip", Path(directory) / "2.zip"
            packing.write_zip(first, packing.pack(layout("ME"), base))
            packing.write_zip(second, dict(reversed(list(base.items()))))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_new_jersey_downloads_through_the_collectors_client(self):
        spec = importlib.util.spec_from_file_location("nj_bundle", ROOT / "scripts/nj_bundle.py")
        nj_bundle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(nj_bundle)
        calls = []

        def fetch(source, settings, target, headers, *, url):
            calls.append((source.source_id, url))
            target.write_bytes(b"summary")
            return FetchResponse(200, {}, url)

        with patch.object(nj_bundle, "fetch_file", side_effect=fetch):
            source = object.__new__(type("S", (), {"source_id": "nj_2026_general"}))
            self.assertEqual(nj_bundle.get("https://example.com/s.json", source, None), b"summary")
        self.assertEqual(calls, [("nj_2026_general", "https://example.com/s.json")])
        with patch.object(
            nj_bundle, "fetch_file", return_value=FetchResponse(403, {}, "https://example.com")
        ):
            with self.assertRaisesRegex(ValueError, "HTTP 403"):
                nj_bundle.get("https://example.com/s.json", source, None)
