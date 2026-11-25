"""One review format, validator and tool; reviews written before it stay readable."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from election_pipeline import reviews

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_tool", ROOT / "scripts/review.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

REPORT = b"%PDF-1.7 Vilas County canvass"
DIGEST = hashlib.sha256(REPORT).hexdigest()
RACES = {"WI-GOV": {"Tom Tiffany": 7000, "Sara Rodriguez": 5000, "Write-in": 12}}


class ReviewTests(unittest.TestCase):
    def test_earlier_illinois_and_wisconsin_reviews_read_as_before(self):
        illinois = {
            "authority": "Adams",
            "file_sha256": DIGEST,
            "reviewed_by": "Naman",
            "races": RACES,
            "totals": {"WI-GOV": 12012},
            "precincts": {"WI-GOV": [10, 10]},
        }
        wisconsin = {"county": "Vilas", "file_sha256": [DIGEST], "reviewed_by": "N", "races": RACES}
        self.assertEqual(reviews.place(illinois), "Adams")
        self.assertEqual(reviews.place(wisconsin), "Vilas")
        found = reviews.read(illinois, [DIGEST], "Adams")
        self.assertEqual(found["WI-GOV"][1], (10, 10))
        self.assertEqual(sum(v for _, v in found["WI-GOV"][0]), 12012)
        self.assertIsNone(reviews.read(wisconsin, [DIGEST], "Vilas")["WI-GOV"][1])

    def test_an_unsigned_mismatched_or_unknown_review_is_refused(self):
        signed = {"place": "Vilas", "file_sha256": [DIGEST], "signed_by": "N", "races": RACES}
        for document, sha256s in (
            ({**signed, "signed_by": " "}, [DIGEST]),
            (signed, ["0" * 64]),
            (signed, [DIGEST, DIGEST]),
            ({**signed, "schema": "election-review/9"}, [DIGEST]),
            ({**signed, "totals": {"WI-GOV": 1}}, [DIGEST]),
            ({**signed, "precincts": {"WI-GOV": [11, 10]}}, [DIGEST]),
            ({**signed, "races": {"WI-GOV": {"Tom Tiffany": -1}}}, [DIGEST]),
        ):
            with self.assertRaises(ValueError):
                reviews.read(document, sha256s, "Vilas")

    def test_the_tool_writes_signs_once_and_checks_a_review(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "vilas.pdf").write_bytes(REPORT)
            transcription = {
                "state": "WI",
                "place": "Vilas",
                "report": "canvassed",
                "transcribed_by": "Claude",
                "races": RACES,
                "totals": {"WI-GOV": 12012},
                "percentages": {"WI-GOV": {"Tom Tiffany": 58.27}},
            }
            (folder / "Vilas.spec.json").write_text(json.dumps(transcription))
            path, totals = tool.write(folder / "Vilas.spec.json", [folder / "vilas.pdf"], folder)
            self.assertEqual(totals, {"WI-GOV": 12012})
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                (written["schema"], written["file_sha256"], written["signed_by"]),
                ("election-review/1", [DIGEST], ""),
            )
            # Unsigned, the parsers refuse it.
            with self.assertRaisesRegex(ValueError, "unsigned"):
                tool.check(path, [folder / "vilas.pdf"])
            tool.sign(path, "Naman", "2026-11-24")
            self.assertEqual(tool.check(path, [folder / "vilas.pdf"]), {"WI-GOV": 12012})
            with self.assertRaisesRegex(ValueError, "already signed"):
                tool.sign(path, "Someone", "2026-11-25")
            with self.assertRaisesRegex(ValueError, "is signed"):
                tool.write(folder / "Vilas.spec.json", [folder / "vilas.pdf"], folder)
            # Counts that disagree with the printed total or percentage are refused.
            for change in (
                {"totals": {"WI-GOV": 12013}},
                {"percentages": {"WI-GOV": {"Tom Tiffany": 60}}},
            ):
                with self.assertRaises(ValueError):
                    reviews.transcription({**transcription, **change}, [DIGEST])
            with self.assertRaisesRegex(ValueError, "lacks"):
                reviews.transcription({"races": RACES}, [DIGEST])
