"""Committed example settings files hold placeholders, never real values.

``.gitignore`` keeps every real ``.env`` out of the repository but deliberately admits the
examples, so an example is the one place a secret can be published by mistake. Every
``*.example`` file must be plain ``KEY=VALUE`` lines (or comments), and a key naming a
secret must hold a placeholder.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted(
    {p for p in [*ROOT.glob(".env*.example"), *ROOT.glob("*/*.example")] if p.is_file()}
)
SECRET = re.compile(r"PASSWORD|SECRET|TOKEN|PRIVATE|CREDENTIAL|ACCESS_KEY")
PLACEHOLDER = re.compile(r"(CHANGE_ME|YOUR_[A-Z0-9_]+|REPLACE_WITH_[A-Z0-9_]+)?")
LINE = re.compile(r"([A-Z][A-Z0-9_]*)=(\S*)")


def problems(text):
    """Line numbers and reasons for every line that breaks the example contract."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = LINE.fullmatch(line.strip())
        if not match:
            found.append((number, "not a KEY=VALUE line"))
            continue
        key, value = match.groups()
        if key.endswith("_PORT") and not re.fullmatch(r"[0-9]{1,5}", value):
            found.append((number, "port is not a number"))
        if SECRET.search(key) and not PLACEHOLDER.fullmatch(value.strip("'\"")):
            found.append((number, f"{key} must be a placeholder such as CHANGE_ME"))
    return found


class ExampleSettingsTests(unittest.TestCase):
    def test_examples_are_found(self):
        names = {p.relative_to(ROOT).as_posix() for p in EXAMPLES}
        self.assertTrue(
            {".env.example", "config/monitor.env.example", "deploy/deployment.env.example"} <= names
        )

    def test_every_example_holds_only_settings_and_placeholders(self):
        for path in EXAMPLES:
            with self.subTest(path.relative_to(ROOT).as_posix()):
                self.assertEqual(problems(path.read_text(encoding="utf-8")), [])

    def test_a_real_secret_or_a_broken_line_is_refused(self):
        self.assertEqual(problems("POSTGRES_PASSWORD=CHANGE_ME\nAPI_TOKEN=\n"), [])
        self.assertEqual(
            [n for n, _ in problems("POSTGRES_PASSWORD='hunter2'\nPOSTGRES_PORT=5432 copy\n")],
            [1, 2],
        )
        self.assertEqual([n for n, _ in problems("SESSION_SECRET=abc123\n")], [1])


if __name__ == "__main__":
    unittest.main()
