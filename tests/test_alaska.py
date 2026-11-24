"""Known answers from Alaska's official capture (CSV, page and the U.S. House race's RCV
report), plus fail-closed mutations."""

import csv
import gzip
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from loading import copies, migrated, requires_database

from election_pipeline import publication
from election_pipeline.dashboard.server import STATIC, make_payload, party_group
from election_pipeline.database import (
    connect,
    load_parsed,
)
from election_pipeline.parsers import ak, ak_rcv
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
# The official capture (Nov 24, 2026); the election-night CSV captured Nov 3 is beside it.
FIXTURE = ROOT / "tests/fixtures/alaska/official"
ELECTION_NIGHT = ROOT / "tests/fixtures/alaska"


class AlaskaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["ak_2026_general"]

    def parse(self, mutate=None, page=None, reference=None, fixture=FIXTURE):
        raw = gzip.decompress((fixture / "results.csv.gz").read_bytes())
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
            zipped.writestr("rcv-usrep.pdf", (FIXTURE / "rcv-usrep.pdf").read_bytes())
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
        self.assertIs(self.parse()["official_results"], True)
        status = b'Results Status: <span style="color: #B30000;">Official</span>'
        self.assertIn(status, page)
        unofficial = page.replace(status, status.replace(b"Official", b"Unofficial"))
        # The page and the tabulation report must agree on whether results are official.
        refused = self.parse(page=unofficial)
        self.assertEqual(refused["outcome"], "validation_failed", refused)
        self.assertIn("official status", refused["error"])
        read = ak_rcv.read
        with patch.object(
            ak.ak_rcv, "read", side_effect=lambda *a: {**read(*a), "official": False}
        ):
            self.assertIs(self.parse(page=unofficial)["official_results"], False)
        blank = self.parse(page=page.replace(status, b"Results Status: <span></span>"))
        self.assertEqual(blank["outcome"], "validation_failed", blank)

    def test_real_capture_scope_methods_residuals_and_replay(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())["files"]
        for name, raw in (
            ("results.csv", gzip.decompress((FIXTURE / "results.csv.gz").read_bytes())),
            ("election.html", (FIXTURE / "election.html").read_bytes()),
            ("rcv-usrep.pdf", (FIXTURE / "rcv-usrep.pdf").read_bytes()),
        ):
            self.assertEqual(hashlib.sha256(raw).hexdigest(), ledger[name]["sha256"], name)
            self.assertEqual(len(raw), ledger[name]["bytes"], name)
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["input_rows"], 20046)
        self.assertEqual(report["selected_rows"], 6272)
        # 12 statewide candidate rows, and each of them in all 40 House districts.
        self.assertEqual(report["output_rows"], 12 + 12 * 40)
        self.assertEqual(
            report["reporting_unit_counts"], {"AK-SEN": 523, "AK-GOV": 522, "AK-AL": 523}
        )
        self.assertEqual(report["ballot_residuals"], {"AK-SEN": 886, "AK-GOV": 828, "AK-AL": 439})
        self.assertEqual(report["ballot_totals"]["AK-SEN"], 290483)
        self.assertEqual(report["vote_method_totals"]["AK-SEN"]["total"], 287479)
        self.assertEqual(report["vote_method_reconciliation"], "passed")
        copied = copies(self.source, self.settings, self.store, report["output_dir"], 77)
        converted = copied[("landing", "results")]
        self.assertEqual(len(converted), 12 + 12 * 40)
        # Three rounds of four, three and two candidates, each with its three
        # non-transferable lines.
        self.assertEqual(len(copied[("landing", "rcv_rounds")]), 4 + 3 + 2 + 3 * 3)
        self.assertIn(None, next(r for r in converted if "Sullivan, Daniel J. Jr." in r))
        replay = parse_source(
            self.source, self.settings, self.root, sha256=report["identity"]["raw_sha256"]
        )
        self.assertEqual(replay["outcome"], "already_parsed")

    def test_exact_candidate_totals_do_not_merge_sullivans_or_aggregate_parties(self):
        report = self.parse()
        totals = {(r["contest_id"], r["choice"]): int(r["total_votes"]) for r in self.read(report)}
        self.assertEqual(totals[("AK-SEN", "Peltola, Mary")], 144583)
        self.assertEqual(totals[("AK-SEN", "Sullivan, Dan S.")], 132022)
        self.assertEqual(totals[("AK-SEN", "Sullivan, Daniel J. Jr.")], 4767)
        self.assertEqual(totals[("AK-GOV", "Kreiss-Tomkins/Johnson")], 145546)
        self.assertEqual(totals[("AK-AL", "Begich, Nick")], 136402)
        geo = json.loads((STATIC / "ak-map.json").read_text())
        districts = [
            {**r, "total_votes": int(r["total_votes"])}
            for r in self.read(report, "county_results.csv")
        ]
        payload = make_payload(
            geo,
            {"status": "unofficial"},
            districts,
            [{**r, "total_votes": int(r["total_votes"])} for r in self.read(report)],
            state="AK",
        )
        self.assertTrue(payload["county_detail"])
        self.assertEqual(payload["local_unit"], "house_district")
        self.assertIn("overseas", payload["statewide_only_votes"])
        senate = payload["races"]["AK-SEN"]
        self.assertEqual(len(senate["counties"]), 40)
        self.assertEqual(senate["counties"]["HOUSEDISTRICT1"]["name"], "House District 1")
        self.assertNotIn("county_privacy_masked", senate)
        # The 40 districts plus the overseas ballots are exactly the statewide total.
        overseas = report["statewide_only_votes"]
        for race in ("AK-SEN", "AK-GOV", "AK-AL"):
            in_districts = sum(r["total_votes"] for r in districts if r["contest_id"] == race)
            statewide = payload["races"][race]["stats"]["total_votes"]["total"]
            self.assertEqual(in_districts + overseas[race], statewide)
        self.assertEqual(overseas["AK-GOV"], 0)  # Governor is not on the federal ballot.
        self.assertGreater(overseas["AK-SEN"], 0)
        # More in the districts than statewide is refused.
        inflated = [dict(r) for r in districts]
        inflated[0]["total_votes"] += overseas[inflated[0]["contest_id"]] + 1
        with self.assertRaises(ValueError):
            make_payload(
                geo,
                {"status": "unofficial"},
                inflated,
                [{**r, "total_votes": int(r["total_votes"])} for r in self.read(report)],
                state="AK",
            )
        self.assertEqual(payload["vote_basis"], "first_choice_listed_candidates")
        self.assertEqual(payload["races"]["AK-SEN"]["stats"]["total_votes"]["margin"], 12561)
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
            page.replace(b"from Round 1", b"from the final round"),
            page.replace(b"State_of_Alaska.csv", b"other.csv"),
            page.replace(b"RCV-USRep.pdf", b"RCV-Other.pdf"),
            b"<html><title>Captcha</title>November 3, 2026</html>",
        ):
            self.assertEqual(self.parse(page=edited)["outcome"], "validation_failed")
        # Official pages can link the same file with a relative URL.
        relative = page.replace(b"https://www.elections.alaska.gov/enr/", b"/enr/")
        self.assertEqual(self.parse(page=relative)["outcome"], "parsed")
        # After its tabulation the page dropped the first-choice label (Nov 18).
        unlabelled = page.replace(
            b"<strong>The Summary Report only shows 1st Choice results from Round 1.</strong>",
            b"",
        )
        self.assertEqual(self.parse(page=unlabelled)["outcome"], "parsed")

    def test_zero_and_downward_correction_are_valid_without_changing_rosters(self):
        def zero(rows):
            for r in rows:
                if r["Contest_Id"] in {"5", "6", "7"}:
                    for prefix in ("total", *ak.METHODS):
                        for measure in ak.MEASURES:
                            r[prefix + "_" + measure] = "0"

        # A zero CSV cannot stand beside a tabulation of its ballots.
        zeroed = self.parse(zero)
        self.assertEqual(zeroed["outcome"], "validation_failed", zeroed)
        self.assertIn("same ballots", zeroed["error"])

        def correction(rows):
            rows[1]["total_votes"] = str(int(rows[1]["total_votes"]) - 1)
            rows[1]["Election Day_votes"] = str(int(rows[1]["Election Day_votes"]) - 1)

        report = self.parse(correction)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["ballot_residuals"]["AK-SEN"], 887)

    def test_the_house_tabulation_is_published_and_checked_against_the_csv(self):
        report = self.parse()
        self.assertEqual(
            report["ranked_choice"],
            {
                "AK-AL": {
                    "rounds": 3,
                    "elected": "Begich, Nick",
                    "elected_votes": 140412,
                    "continuing_ballots": 273456,
                    "tabulated_at": "11/23/2026 10:04:21 AM",
                    "ballots": 290483,
                }
            },
        )
        rounds = self.read(report, "rcv_rounds.csv")
        final = {
            r["choice"]: (r["votes"], r["outcome"])
            for r in rounds
            if r["round"] == "3" and r["kind"] == "candidate"
        }
        self.assertEqual(final["Begich, Nick"], ("140412", "elected"))
        self.assertEqual(final["Hill, Bill"], ("133044", "defeated"))
        exhausted = [r["votes"] for r in rounds if r["kind"] == "exhausted"]
        self.assertEqual(exhausted, ["0", "2522", "13995"])
        # The election-night CSV counts other ballots than the official tabulation.
        night = self.parse(fixture=ELECTION_NIGHT)
        self.assertEqual(night["outcome"], "validation_failed", night)
        self.assertIn("same ballots", night["error"])

    def test_the_dashboard_shows_the_rounds_of_the_tabulated_race_alone(self):
        report = self.parse()
        geo = json.loads((STATIC / "ak-map.json").read_text())
        contests = [{**r, "total_votes": int(r["total_votes"])} for r in self.read(report)]
        districts = [
            {**r, "total_votes": int(r["total_votes"])}
            for r in self.read(report, "county_results.csv")
        ]
        rounds = [
            {
                **r,
                "round": int(r["round"]),
                "votes": int(r["votes"]),
                "outcome": r["outcome"] or None,
            }
            for r in self.read(report, "rcv_rounds.csv")
        ]

        def payload(rows):
            return make_payload(
                geo, {"status": "official"}, districts, contests, state="AK", rcv_rows=rows
            )

        house = payload(rounds)["races"]["AK-AL"]["ranked_choice"]
        self.assertEqual((house["elected"], house["final_round"]), ("Begich, Nick", 3))
        last = house["rounds"][-1]
        self.assertEqual(
            [(c["name"], c["votes"], c["outcome"]) for c in last["candidates"]],
            [("Begich, Nick", 140412, "elected"), ("Hill, Bill", 133044, "defeated")],
        )
        self.assertEqual((last["continuing"], last["exhausted"]), (273456, 13995))
        self.assertEqual(last["candidates"][0]["party"], "REP")
        self.assertNotIn("ranked_choice", payload(rounds)["races"]["AK-SEN"])
        # A publication made before the tabulation was imported has none.
        self.assertNotIn("ranked_choice", payload([])["races"]["AK-AL"])
        for rows in (
            [{**r, "contest_id": "AK-SEN"} for r in rounds],
            [r for r in rounds if r["round"] != 2],
            [r for r in rounds if r["outcome"] != "elected"],
        ):
            with self.assertRaises(ValueError):
                payload(rows)

    def test_a_tabulation_that_does_not_add_up_is_refused(self):
        lines = ak_rcv.lines_of((FIXTURE / "rcv-usrep.pdf").read_bytes(), self.settings)

        def changed(old, new, occurrence=0):
            edited, seen = list(lines), -1
            for index, line in enumerate(edited):
                if line == old:
                    seen += 1
                    if seen == occurrence:
                        edited[index] = new
                        return edited
            raise AssertionError(old)

        for edited in (
            changed("140,412", "140,413"),  # round 3 no longer the transfers' sum
            changed("2,061", "2,062"),  # a transfer that moves no ballots
            changed("Official results", "Draft results"),
            changed("All Positions Filled", "In Progress"),
            changed("IRV", "STV"),
            changed("elected", "eliminated"),  # nobody elected in the last round
            changed("2,782", "2,783", occurrence=1),  # blanks change between rounds
            changed("47.56%", "47.65%"),
        ):
            with (
                self.subTest(edited=edited[:0]),
                patch.object(ak.ak_rcv, "lines_of", return_value=edited),
            ):
                result = self.parse()
                self.assertEqual(result["outcome"], "validation_failed", result)

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


@requires_database
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
            rounds = conn.execute(
                "SELECT round, choice, votes FROM serving.rcv_rounds "
                "WHERE publication_id=%s AND outcome='elected'",
                (published["publication_id"],),
            ).fetchall()
            self.assertEqual(
                [(r["round"], r["choice"], r["votes"]) for r in rounds],
                [(3, "Begich, Nick", 140412)],
            )
            conn.execute("SET ROLE election_dashboard")
            for table in ("serving.contest_results", "serving.rcv_rounds"):
                self.assertTrue(
                    conn.execute(
                        "SELECT has_table_privilege(current_user, %s, 'SELECT') AS ok", (table,)
                    ).fetchone()["ok"],
                    table,
                )
            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                ).fetchone()["ok"]
            )
