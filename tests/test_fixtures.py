"""Every fixture file is the one its provenance records.

A fixture folder's ``provenance.json`` says where its files came from: a capture's URL,
time and method, or that a file was written for the tests. Its shape varies with the
batch that added it, so this check asks one question of every file: does the nearest
``provenance.json`` record its SHA-256 (or, for a ``.gz``, that of what it holds; for a
text file git stores with LF line endings, that of the LF form)? A replaced or edited
fixture then fails here, in whichever folder it is.
"""

import gzip
import hashlib
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNRECORDED = {"provenance.json", "README.md"}


def ledger_for(path):
    """The nearest provenance.json at or above ``path``'s folder, within the fixtures."""
    for folder in [path.parent, *path.parent.parents]:
        if (folder / "provenance.json").is_file():
            return folder / "provenance.json"
        if folder == FIXTURES:
            break
    return None


class FixtureProvenanceTests(unittest.TestCase):
    def test_every_fixture_folder_has_a_provenance_record(self):
        for folder in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
            with self.subTest(folder=folder.name):
                self.assertTrue((folder / "provenance.json").is_file())

    def test_every_fixture_file_has_the_sha256_its_provenance_records(self):
        files = [p for p in FIXTURES.rglob("*") if p.is_file() and p.name not in UNRECORDED]
        self.assertGreater(len(files), 50)
        for path in sorted(files):
            with self.subTest(file=path.relative_to(FIXTURES).as_posix()):
                ledger = ledger_for(path)
                self.assertIsNotNone(ledger)
                raw = path.read_bytes()
                hashes = {hashlib.sha256(raw).hexdigest()}
                if path.suffix == ".gz":
                    hashes.add(hashlib.sha256(gzip.decompress(raw)).hexdigest())
                # Git stores a text file (no NUL in its first 8000 bytes) with LF line
                # endings and may check it out with CRLF; its record is of the LF form.
                if b"\0" not in raw[:8000]:
                    hashes.add(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest())
                self.assertTrue(
                    hashes & set(_hex_values(ledger.read_text(encoding="utf-8"))),
                    f"{ledger.relative_to(FIXTURES)} does not record this file",
                )


def _hex_values(text):
    import re

    return re.findall(r"\b[0-9a-f]{64}\b", text)


if __name__ == "__main__":
    unittest.main()
