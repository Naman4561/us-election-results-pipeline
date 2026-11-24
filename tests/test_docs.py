"""The docs that describe the whole registry stay in step with it."""

import re
import unittest
from pathlib import Path

from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]


class DocsTests(unittest.TestCase):
    def test_the_state_counts_match_the_registry(self):
        count = len(STATES)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"race of all {count} states", " ".join(readme.split()))
        architecture = (ROOT / "docs/architecture/README.md").read_text(encoding="utf-8")
        self.assertIn(f"from {count} state election offices", " ".join(architecture.split()))

    def test_the_platform_diagram_lists_every_state_once(self):
        text = (ROOT / "docs/architecture/data-collection.md").read_text(encoding="utf-8")
        diagram = text.split("## Source platforms", 1)[1].split("```", 2)[1]
        listed = []
        for line in diagram.splitlines():
            match = re.fullmatch(r"\s*([A-Z]{2}(?: & [A-Z]{2})*) --> \w+", line)
            if match:
                listed += match.group(1).split(" & ")
        self.assertEqual(sorted(listed), sorted(STATES))


if __name__ == "__main__":
    unittest.main()
