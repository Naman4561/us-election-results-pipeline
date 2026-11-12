"""scripts/check_pipeline.py: which tests the database jobs run."""

import re
import unittest
from pathlib import Path

import yaml
from loading import requires_database

from scripts import check_pipeline

ROOT = Path(__file__).resolve().parents[1]


def names(tests):
    return sorted(f"{type(t).__name__}.{t._testMethodName}" for t in tests)


class CheckPipelineTests(unittest.TestCase):
    def setUp(self):
        # Defined here, not at module level, so the real database run does not find them.
        class Plain(unittest.TestCase):
            def test_a(self):
                pass

        @requires_database
        class First(unittest.TestCase):
            def test_a(self):
                pass

            def test_b(self):
                pass

        @requires_database
        class Second(unittest.TestCase):
            def test_a(self):
                pass

        @requires_database
        class Third(unittest.TestCase):
            def test_a(self):
                pass

        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite(
            loader.loadTestsFromTestCase(c) for c in (Plain, First, Second, Third)
        )
        self.tests = list(check_pipeline.flatten(suite))

    def test_database_only_shards_split_whole_classes_and_cover_each_once(self):
        everything = names(check_pipeline.selected(self.tests, True, (1, 1)))
        self.assertEqual(
            everything, ["First.test_a", "First.test_b", "Second.test_a", "Third.test_a"]
        )
        parts = [names(check_pipeline.selected(self.tests, True, (i, 2))) for i in (1, 2)]
        self.assertEqual(sorted(parts[0] + parts[1]), everything)
        classes = [{n.split(".")[0] for n in part} for part in parts]
        self.assertTrue(all(classes))
        self.assertFalse(classes[0] & classes[1])

    def test_without_database_only_every_test_runs_and_a_broken_module_always_runs(self):
        self.assertEqual(len(check_pipeline.selected(self.tests, False, (1, 1))), 5)
        broken = unittest.loader._FailedTest("test_gone", ImportError("gone"))
        self.assertIn(broken, check_pipeline.selected([broken, *self.tests], True, (2, 2)))

    def test_shard_argument(self):
        self.assertEqual(check_pipeline.shard("2/3"), (2, 3))
        for text in ("0/2", "3/2", "a/b", "2"):
            with self.assertRaises(Exception):
                check_pipeline.shard(text)


def ci_jobs():
    """The check-run names ci.yml produces, as GitHub names them."""
    names = set()
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for key, job in workflow["jobs"].items():
        matrix = job.get("strategy", {}).get("matrix", {})
        (axis, values), *rest = matrix.items() if matrix else [(None, [None])]
        assert not rest, "one matrix axis per job"
        for value in values:
            name = job.get("name", key)
            if axis and "${{" in name:
                name = name.replace("${{ matrix.%s }}" % axis, str(value))
            elif axis:
                name = f"{name} ({value})"
            names.add(name)
    return names


def gate(script):
    text = (ROOT / script).read_text(encoding="utf-8")
    found = re.search(r"^required = \{(.*?)\}", text, re.S | re.M)
    return set(re.findall(r'"([^"]+)"', found[1]))


class ShipGateTests(unittest.TestCase):
    def test_stage_and_ship_wait_for_the_same_existing_ci_jobs(self):
        jobs = ci_jobs()
        self.assertEqual(
            jobs,
            {
                "test (ubuntu-latest)",
                "test (windows-latest)",
                "dashboard",
                "postgres-1",
                "postgres-2",
            },
        )
        ship, stage = gate("scripts/ship_release.sh"), gate("scripts/stage_release.sh")
        self.assertEqual(ship, stage)
        self.assertLessEqual(ship, jobs)
        # Production runs Linux; every job but the Windows suite holds a release.
        self.assertEqual(ship, jobs - {"test (windows-latest)"})
