"""scripts/check_pipeline.py: which tests the database jobs run."""

import unittest

from loading import requires_database

from scripts import check_pipeline


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
