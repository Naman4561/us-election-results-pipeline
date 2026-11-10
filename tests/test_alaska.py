"""Known answers from the uploaded official CSV, plus fail-closed mutations."""

import csv
import gzip
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from loading import copies, migrated

from election_pipeline import publication
from election_pipeline.dashboard.server import STATIC, make_payload, party_group
from election_pipeline.database import (
    connect,
    load_parsed,
)
from election_pipeline.parsers import ak
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/alaska"


class AlaskaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ak_2026_general"]

    def parse(self, mutate=None, page=None, reference=None):
        raw = gzip.decompress((FIXTURE / "results.csv.gz").read_bytes())
        if mutate:
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            mutate(rows)
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, [*ak.HEADER, ""])
            writer.writeheader()
            writer.writerows(rows)
            raw = stream.getvalue().encode("utf-8")
        archive = self.root / "input.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("results.csv", raw)
            zipped.writestr("election.html", page or (FIXTURE / "election.html").read_bytes())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.store = ArchiveStore(self.root, self.source)
        target = self.store.archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read_bytes())
        return parse_source(
            self.source, self.settings, self.root, sha256=digest, reference=reference
        )

    def read(self, report, name="contest_results.csv"):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_official_only_from_the_pages_results_status(self):
        page = (FIXTURE / "election.html").read_bytes()
        self.assertIs(self.parse()["official_results"], False)
        status = b'Results Status: <span style="color: #B30000;">Unofficial</span>'
        self.assertIn(status, page)
        official = self.parse(page=page.replace(status, status.replace(b"Unofficial", b"Official")))
        self.assertIs(official["official_results"], True)
        blank = self.parse(page=page.replace(status, b"Results Status: <span></span>"))
        self.assertEqual(blank["outcome"], "validation_failed", blank)

    def test_real_capture_scope_methods_residuals_and_replay(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        raw = gzip.decompress((FIXTURE / "results.csv.gz").read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ledger["sha256"])
        self.assertEqual(len(raw), ledger["bytes"])
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["input_rows"], 20046)
        self.assertEqual(report["selected_rows"], 6272)
        self.assertEqual(report["output_rows"], 12)
        self.assertEqual(
            report["reporting_unit_counts"], {"AK-SEN": 523, "AK-GOV": 522, "AK-AL": 523}
        )
        self.assertEqual(report["ballot_residuals"], {"AK-SEN": 721, "AK-GOV": 656, "AK-AL": 420})
        self.assertEqual(report["ballot_totals"]["AK-SEN"], 215335)
        self.assertEqual(report["vote_method_totals"]["AK-SEN"]["total"], 213180)
        self.assertEqual(report["vote_method_reconciliation"], "passed")
        converted = copies(self.source, self.settings, self.store, report["output_dir"], 77)[
            ("landing", "results")
        ]
        self.assertEqual(len(converted), 12)
        self.assertIn(None, next(r for r in converted if "Sullivan, Daniel J. Jr." in r))
        replay = parse_source(
            self.source, self.settings, self.root, sha256=report["identity"]["raw_sha256"]
        )
        self.assertEqual(replay["outcome"], "already_parsed")

    def test_exact_candidate_totals_do_not_merge_sullivans_or_aggregate_parties(self):
        report = self.parse()
        totals = {(r["contest_id"], r["choice"]): int(r["total_votes"]) for r in self.read(report)}
        self.assertEqual(totals[("AK-SEN", "Peltola, Mary")], 102701)
        self.assertEqual(totals[("AK-SEN", "Sullivan, Dan S.")], 102075)
        self.assertEqual(totals[("AK-SEN", "Sullivan, Daniel J. Jr.")], 3583)
        self.assertEqual(totals[("AK-GOV", "Kreiss-Tomkins/Johnson")], 103610)
        self.assertEqual(totals[("AK-AL", "Begich, Nick")], 105353)
        geo = json.loads((STATIC / "ak-map.json").read_text())
        payload = make_payload(
            geo,
            {"status": "unofficial"},
            [],
            [{**r, "total_votes": int(r["total_votes"])} for r in self.read(report)],
            state="AK",
        )
        self.assertFalse(payload["county_detail"])
        self.assertEqual(payload["vote_basis"], "first_choice_listed_candidates")
        self.assertEqual(payload["races"]["AK-SEN"]["stats"]["total_votes"]["margin"], 626)
        self.assertEqual(payload["races"]["AK-GOV"]["stats"]["total_votes"]["leader"], "DEM")
        self.assertEqual(party_group("DEM/NON", "Kreiss-Tomkins/Johnson"), "DEM")
        self.assertEqual(party_group("", "Sullivan, Daniel J. Jr."), "UNKNOWN")
        self.assertEqual([d["id"] for d in geo["districts"]], ["AK-AL"])

    def test_missing_unit_blank_id_unit_candidate_duplicate_and_aggregate_fail(self):
        edits = [
            lambda r: r.__setitem__(slice(None), [x for x in r if x["Pct_seq_nbr"] != "444"]),
            lambda r: r.__setitem__(
                slice(None),
                [x for x in r if not (x["Pct_seq_nbr"] == "1" and x["Contest_Id"] == "6")],
            ),
            lambda r: r.pop(0),
            lambda r: r.append(dict(r[0])),
            lambda r: r[0].update(Aggregation_ID="state-total"),
            lambda r: r[0].update(Party_Code="REP/DEM"),
            lambda r: r[0].update(candidate_id="999999"),
            lambda r: r[0].update(Precinct_name="Another precinct"),
            lambda r: r[0].update(candidate_name="Name\0"),
            lambda r: r[0].update(total_votes=""),
            lambda r: r[0].update(total_votes="-1"),
        ]
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                self.assertEqual(self.parse(edit)["outcome"], "validation_failed")

    def test_method_mismatch_and_within_unit_ballot_metadata_fail(self):
        for edit in (
            lambda r: r[0].update(total_votes=str(int(r[0]["total_votes"]) + 1)),
            lambda r: r[0].update(total_ballots="492", **{"Election Day_ballots": "492"}),
            lambda r: r[0].update(total_votes="491", **{"Election Day_votes": "491"}),
        ):
            self.assertEqual(self.parse(edit)["outcome"], "validation_failed")

    def test_wrong_election_challenge_changed_link_and_round_fail(self):
        page = (FIXTURE / "election.html").read_bytes()
        for edited in (
            page.replace(b"2026 General Election", b"2024 General Election"),
            page.replace(b"Round 1", b"Round 2"),
            page.replace(b"State_of_Alaska.csv", b"other.csv"),
            b"<html><title>Captcha</title>November 3, 2026</html>",
        ):
            self.assertEqual(self.parse(page=edited)["outcome"], "validation_failed")
        # Official pages can link the same file with a relative URL.
        relative = page.replace(b"https://www.elections.alaska.gov/enr/", b"/enr/")
        self.assertEqual(self.parse(page=relative)["outcome"], "parsed")

    def test_zero_and_downward_correction_are_valid_without_changing_rosters(self):
        def zero(rows):
            for r in rows:
                if r["Contest_Id"] in {"5", "6", "7"}:
                    for prefix in ("total", *ak.METHODS):
                        for measure in ak.MEASURES:
                            r[prefix + "_" + measure] = "0"

        self.assertTrue(self.parse(zero)["all_counts_zero"])

        def correction(rows):
            rows[1]["total_votes"] = str(int(rows[1]["total_votes"]) - 1)
            rows[1]["Election Day_votes"] = str(int(rows[1]["Election Day_votes"]) - 1)

        report = self.parse(correction)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["ballot_residuals"]["AK-SEN"], 722)

    def test_wrong_source_and_scope_resource_fail(self):
        for source in (
            replace(self.source, election_date="2024-11-05"),
            replace(self.source, url=ak.CSV_URL.replace("26GENR", "24GENR")),
            replace(self.source, min_interval_seconds=0),
        ):
            with self.assertRaises(ValueError):
                ak.validate_source(source)
        with (
            patch.object(ak, "SCOPE_SHA256", "0" * 64),
            self.assertRaisesRegex(ValueError, "integrity"),
        ):
            ak.scope()


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class AlaskaDatabaseTests(unittest.TestCase):
    def test_load_dbt_publication_reader_permissions_and_idempotent_replay(self):
        db = migrated()
        with connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        fixture = AlaskaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        report = fixture.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        loaded = load_parsed(
            fixture.source, fixture.settings, fixture.root, Path(report["output_dir"]), db
        )
        self.assertEqual(loaded["outcome"], "succeeded", loaded)
        prepared = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        published = publication.publish_prepared(ROOT, db, prepared["candidate_id"])
        self.assertEqual(published["outcome"], "published", published)
        self.assertEqual(
            publication.publish(ROOT, db, loaded["release_id"], "unofficial", skip_current=True)[
                "outcome"
            ],
            "already_current",
        )
        with connect(db) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) AS n FROM serving.contest_results WHERE state='AK'"
                ).fetchone()["n"],
                12,
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT choice_party FROM serving.contest_results WHERE choice=%s",
                    ("Sullivan, Daniel J. Jr.",),
                ).fetchone()["choice_party"]
            )
            conn.execute("SET ROLE election_dashboard")
            self.assertTrue(
                conn.execute(
                    "SELECT has_table_privilege(current_user, 'serving.contest_results',"
                    " 'SELECT') AS ok"
                ).fetchone()["ok"]
            )
            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                ).fetchone()["ok"]
            )
