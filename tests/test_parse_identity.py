"""A parse is identified by the code and data its state depends on (step 3.9, R-015)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from election_pipeline import pipeline
from election_pipeline.parsing import parse_source  # noqa: F401  (the parse step is a root)

ROOT = Path(__file__).resolve().parents[1]
ELECTION = "config/elections/2026-general"


class ParseIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.release = Path(directory.name)
        # A copy: the installed package is read-only in the release image.
        shutil.copytree(
            ROOT / "src/election_pipeline",
            self.release / "src/election_pipeline",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copytree(ROOT / "config", self.release / "config")
        self.package = self.release / "src/election_pipeline"
        self.config = self.release / "config"

    def hash(self, code):
        return pipeline.parse_code_hash(code, self.package, self.config)

    def changed(self, relative, base, code):
        path = self.release / relative
        original = path.read_bytes()
        before = self.hash(code)
        path.write_bytes(original + base)
        try:
            return self.hash(code) != before
        finally:
            path.write_bytes(original)

    def test_the_copy_hashes_as_the_installed_package_does(self):
        for code in ("HI", "NC", "FL"):
            self.assertEqual(pipeline.parse_code_hash(code), self.hash(code))

    def test_another_states_parser_roster_and_county_table_are_not_part_of_it(self):
        self.assertFalse(self.changed("src/election_pipeline/parsers/nc.py", b"\n# x\n", "HI"))
        self.assertFalse(self.changed(f"{ELECTION}/rosters/NC.json", b" ", "HI"))
        self.assertFalse(self.changed(f"{ELECTION}/counties/NC.json", b" ", "HI"))
        self.assertFalse(self.changed(f"{ELECTION}/states/NC.toml", b"\n# x\n", "HI"))

    def test_its_own_parser_data_and_county_table_are(self):
        self.assertTrue(self.changed("src/election_pipeline/parsers/hi.py", b"\n# x\n", "HI"))
        self.assertTrue(self.changed(f"{ELECTION}/rosters/HI.json", b" ", "HI"))
        self.assertTrue(self.changed(f"{ELECTION}/counties/NC.json", b" ", "NC"))
        self.assertTrue(self.changed(f"{ELECTION}/states/HI.toml", b"\n# x\n", "HI"))

    def test_shared_code_and_the_election_file_are_part_of_every_states(self):
        for relative in (
            "src/election_pipeline/parsers/results.py",
            "src/election_pipeline/parsing.py",
            "src/election_pipeline/counties.py",
            f"{ELECTION}/election.toml",
        ):
            for code in ("HI", "NC"):
                with self.subTest(relative=relative, code=code):
                    self.assertTrue(self.changed(relative, b"\n# x\n", code))

    def test_code_no_parser_imports_is_not_part_of_it(self):
        # The dashboard never takes part in a parse.
        self.assertFalse(
            self.changed("src/election_pipeline/dashboard/server.py", b"\n# x\n", "HI")
        )

    def test_the_whole_package_hash_still_names_the_release(self):
        before = pipeline.collector_code_hash(self.package, self.config)
        server = self.package / "dashboard/server.py"
        server.write_bytes(server.read_bytes() + b"\n# x\n")
        self.assertNotEqual(pipeline.collector_code_hash(self.package, self.config), before)


if __name__ == "__main__":
    unittest.main()
