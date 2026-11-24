"""No committed file publishes infrastructure details (scripts/check_public_values.py):
public IP addresses, S3 buckets, AWS ARNs or access keys. The pre-commit hook runs the same
check over the lines a commit adds."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check", ROOT / "scripts/check_public_values.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

# Built at run time so that this file holds none of them itself.
PUBLIC4 = ".".join(["8", "8", "4", "4"])
PUBLIC6 = ":".join(["2606", "4700", "4700", "", "1111"])


class PublicValuesTests(unittest.TestCase):
    def test_no_tracked_file_publishes_infrastructure_details(self):
        for name, text in check.tracked():
            with self.subTest(name):
                self.assertEqual(check.problems(text), [], f"{name} publishes infrastructure")

    def test_the_check_finds_each_kind(self):
        problems = check.problems
        self.assertEqual(problems(f"ssh ubuntu@{PUBLIC4}"), ["public IPv4 address"])
        self.assertEqual(problems(f"[{PUBLIC6}]:22"), ["public IPv6 address"])
        self.assertEqual(problems("s3://" + "election-backups-prod-x"), ["S3 bucket"])
        self.assertEqual(
            problems("arn:aws:iam::" + "1" * 12 + ":user/x"), ["AWS ARN with an account ID"]
        )
        self.assertEqual(problems("AKIA" + "A" * 16), ["AWS access key ID"])

    def test_private_documentation_and_example_values_pass(self):
        allowed = (
            "127.0.0.1 10.0.0.5 172.26.3.1 192.168.1.1 192.0.2.10 198.51.100.7 203.0.113.9 "
            "::1 fe80::1 2001:db8::5 s3://private-bucket s3://example-bucket "
            "version 2.11.7 at 19:05:14 on 2026-11-24"
        )
        self.assertEqual(check.problems(allowed), [])

    def test_a_commit_is_judged_by_the_lines_it_adds_outside_fixtures(self):
        diff = "\n".join(
            [
                "diff --git a/docs/notes.md b/docs/notes.md",
                "--- a/docs/notes.md",
                "+++ b/docs/notes.md",
                "@@ -1 +1 @@",
                f"-old line naming {PUBLIC4}",
                "+a harmless new line",
                "diff --git a/docs/deploy.md b/docs/deploy.md",
                "--- /dev/null",
                "+++ b/docs/deploy.md",
                "@@ -0,0 +1 @@",
                f"+ssh ubuntu@{PUBLIC4}",
                "diff --git a/tests/fixtures/x/page.html b/tests/fixtures/x/page.html",
                "--- /dev/null",
                "+++ b/tests/fixtures/x/page.html",
                "@@ -0,0 +1 @@",
                f"+<a>{PUBLIC4}</a>",
            ]
        )
        found = {name: check.problems(text) for name, text in check.added(diff)}
        self.assertEqual(found, {"docs/notes.md": [], "docs/deploy.md": ["public IPv4 address"]})


if __name__ == "__main__":
    unittest.main()
