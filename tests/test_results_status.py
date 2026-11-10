import unittest

from election_pipeline.parsers.status import results_status

LABEL = "{} Results General Election November 3, 2026"


class ResultsStatusTests(unittest.TestCase):
    def test_reads_unofficial_and_official_labels(self):
        self.assertFalse(
            results_status("Unofficial Results\n General Election November 3, 2026", LABEL, "SD")
        )
        self.assertTrue(
            results_status("Official Results General Election November 3, 2026", LABEL, "SD")
        )

    def test_official_is_not_read_inside_unofficial(self):
        self.assertFalse(
            results_status("x Unofficial Results General Election November 3, 2026", LABEL, "SD")
        )

    def test_refuses_neither_or_both(self):
        for body in (
            "Certified Results General Election November 3, 2026",
            "Unofficial Results General Election November 3, 2026 "
            "Official Results General Election November 3, 2026",
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "label"):
                results_status(body, LABEL, "SD")

    def test_label_elsewhere_on_the_page_does_not_count(self):
        body = "Unofficial write-in results Official General Election November 3, 2026"
        self.assertTrue(results_status(body, "{} General Election November 3, 2026", "OR"))


if __name__ == "__main__":
    unittest.main()
