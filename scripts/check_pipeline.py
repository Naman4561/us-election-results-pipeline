"""Run the suite in a newly created disposable PostgreSQL database."""

import argparse
import os
import sys
import unittest
import uuid
from pathlib import Path

from psycopg import sql

from election_pipeline.database import connect, connection_settings


def shard(text):
    index, _, count = text.partition("/")
    if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        raise argparse.ArgumentTypeError("shard must be I/N with 1 <= I <= N")
    return int(index), int(count)


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def selected(tests, database_only, part):
    """The tests to run: a module that fails to import always runs, so its error shows.
    Shards take whole classes in turn (each migrates once): most database classes are one
    long test, so the number of classes, not of tests, is what is balanced."""
    broken = [t for t in tests if isinstance(t, unittest.loader._FailedTest)]
    classes = {}
    for test in tests:
        if test in broken or (database_only and not getattr(test, "requires_database", False)):
            continue
        classes.setdefault(f"{type(test).__module__}.{type(test).__qualname__}", []).append(test)
    index, count = part
    return broken + [
        t for i, n in enumerate(sorted(classes)) if i % count == index - 1 for t in classes[n]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py", help="Run only matching test modules")
    parser.add_argument(
        "--database-only",
        action="store_true",
        help="Run only the tests that need PostgreSQL (loading.requires_database)",
    )
    parser.add_argument(
        "--shard", type=shard, default=(1, 1), help="Run part I of N of the tests, e.g. 1/2"
    )
    parser.add_argument("--durations", type=int, default=25, help="Report the N slowest tests")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    db = connection_settings(root / ".env")

    name = "election_acceptance_" + uuid.uuid4().hex[:16] + "_test"

    created = False
    code = 1

    try:
        with connect({**db, "dbname": "postgres"}) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
            created = True

        # Test modules read it when they are imported, so it is set before discovery.
        os.environ["ELECTION_TEST_DATABASE"] = name
        # As ``python -m unittest`` from the root: tests import ``scripts`` and ``loading``.
        os.chdir(root)
        sys.path.insert(0, str(root))
        tests = list(flatten(unittest.defaultTestLoader.discover("tests", pattern=args.pattern)))
        chosen = selected(tests, args.database_only, args.shard)
        if args.database_only and not chosen:
            raise ValueError("No database tests selected; is loading.requires_database applied?")
        print(
            f"Running {len(chosen)} of {len(tests)} tests (shard {args.shard[0]}/{args.shard[1]})"
        )
        result = unittest.TextTestRunner(verbosity=2, durations=args.durations).run(
            unittest.TestSuite(chosen)
        )
        code = 0 if result.wasSuccessful() else 1

    except Exception as error:
        print(
            str(error).replace(db["password"], "[redacted]"),
            file=sys.stderr,
        )

    finally:
        if created:
            try:
                with connect({**db, "dbname": "postgres"}) as conn:
                    # The tests ran in this process; FORCE ends any connection one left open.
                    conn.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                    )

            except Exception as error:
                code = 1
                print(
                    f"Temporary DB {name} needs cleanup: "
                    + str(error).replace(db["password"], "[redacted]"),
                    file=sys.stderr,
                )

    return code


if __name__ == "__main__":
    raise SystemExit(main())
