"""Refuse infrastructure details in the public repository: public IP addresses, S3 buckets,
AWS ARNs with an account ID, and access key IDs.

The production server's address was once published in two rollout notes. CI runs this over
every tracked file (``tests/test_public_values.py``), but CI only sees a commit after it is
pushed, so ``scripts/hooks/pre-commit`` runs it over the lines a commit adds, before the
commit exists. It needs no list of private values and prints only what kind of detail it
found and where, never the value. Captured official files under ``tests/fixtures`` are
exempt: they are the states' own bytes and may print anything.

    python scripts/check_public_values.py            # every tracked file
    python scripts/check_public_values.py --staged   # the lines the next commit adds

Standard library only, so the hook can run it with any Python 3.
"""

import argparse
import ipaddress
import re
import subprocess
import sys
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


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout


def tracked():
    """(path, text) for every tracked text file outside the exempt fixtures."""
    for name in filter(None, git("ls-files", "-z").decode("utf-8").split("\0")):
        path = ROOT / name
        if name.startswith(EXEMPT) or not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            yield name, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary


def added(diff):
    """(path, text of its added lines) from a unified diff."""
    files, name = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            name = target[2:] if target.startswith("b/") else None
        elif line.startswith("+") and name and not name.startswith(EXEMPT):
            files.setdefault(name, []).append(line[1:])
    return [(path, "\n".join(lines)) for path, lines in files.items()]


def staged():
    diff = git("diff", "--cached", "--no-color", "--no-ext-diff", "-U0", "--diff-filter=ACMR")
    return added(diff.decode("utf-8", errors="replace"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--staged", action="store_true", help="check the lines being committed")
    args = parser.parse_args()
    found = {
        name: kinds
        for name, text in (staged() if args.staged else tracked())
        if (kinds := problems(text))
    }
    for name, kinds in sorted(found.items()):
        print(f"{name}: {', '.join(sorted(set(kinds)))}", file=sys.stderr)
    if found:
        print(
            "Infrastructure details must not be committed to this public repository; "
            "remove them and try again.",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
