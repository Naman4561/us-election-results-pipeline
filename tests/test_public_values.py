"""No committed file publishes infrastructure details: public IP addresses, S3 buckets,
AWS ARNs or access keys.

The repository is public, and the production server's address was once published in two
rollout notes. This check covers what is general (any public address, any real-looking
bucket); ``scripts/hooks/pre-commit`` checks the exact private values, which are kept
outside the repository. Captured official files under ``tests/fixtures`` are exempt: they
are the states' own bytes and may print anything.
"""

import ipaddress
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXEMPT = ("tests/fixtures/",)
IPV4 = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])")
IPV6 = re.compile(
    r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{1,4})*(?![\w:])"
)
BUCKET = re.compile(r"s3://([a-z0-9][a-z0-9.-]{2,62})")
# Bucket names the tests and docs use as stand-ins.
EXAMPLE_BUCKETS = re.compile(r"(?:private|example|test|your|my)[a-z0-9-]*|bucket[a-z0-9-]*")
ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:")
ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def problems(text):
    """The kinds of infrastructure detail ``text`` publishes (values not included)."""
    found = []
    for match in IPV4.findall(text):
        try:
            if ipaddress.IPv4Address(match).is_global:
                found.append("public IPv4 address")
        except ValueError:
            pass
    for match in IPV6.findall(text):
        try:
            if ipaddress.IPv6Address(match).is_global:
                found.append("public IPv6 address")
        except ValueError:
            pass
    found += ["S3 bucket" for b in BUCKET.findall(text) if not EXAMPLE_BUCKETS.fullmatch(b)]
    found += ["AWS ARN with an account ID"] * len(ARN.findall(text))
    found += ["AWS access key ID"] * len(ACCESS_KEY.findall(text))
    return found


def tracked():
    names = (
        subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
        .stdout.decode("utf-8")
        .split("\0")
    )
    for name in filter(None, names):
        path = ROOT / name
        if name.startswith(EXEMPT) or not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary


class PublicValuesTests(unittest.TestCase):
    def test_no_tracked_file_publishes_infrastructure_details(self):
        for name, text in tracked():
            with self.subTest(name):
                self.assertEqual(problems(text), [], f"{name} publishes infrastructure details")

    def test_the_check_finds_each_kind(self):
        # Built at run time so that this file holds none of them itself.
        public4 = ".".join(["8", "8", "4", "4"])
        public6 = ":".join(["2606", "4700", "4700", "", "1111"])
        self.assertEqual(problems(f"ssh ubuntu@{public4}"), ["public IPv4 address"])
        self.assertEqual(problems(f"[{public6}]:22"), ["public IPv6 address"])
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
        self.assertEqual(problems(allowed), [])


if __name__ == "__main__":
    unittest.main()
