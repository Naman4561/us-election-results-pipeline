"""NC analysis view: known answers, integrity checks, demo labelling and reader access."""

import copy
import json
import os
import unittest
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    from election_pipeline.dashboard.analysis import (
        NCAnalysis,
        build_analysis,
        display_name,
    )
    from election_pipeline.dashboard.server import STATIC, create_app, party_group
except ImportError:
    TestClient = None

METHODS = ("election_day", "early_voting", "absentee_by_mail", "provisional")


def result(contest, choice, party, ed, early, mail, prov, county=None):
    row = dict(
        contest_id=contest,
        choice=choice,
        choice_party=party,
        election_day=ed,
        early_voting=early,
        absentee_by_mail=mail,
        provisional=prov,
        total_votes=ed + early + mail + prov,
    )
    return {**row, "county": county} if county else row


def ballots(kind, party, sex, race, ethnicity, count):
    return dict(
        request_type=kind,
        party_registration=party,
        sex=sex,
        race=race,
        ethnicity=ethnicity,
        ballot_count=count,
    )


@unittest.skipUnless(TestClient, "Install postgres, dashboard extras and httpx")
class AnalysisPayloadTests(unittest.TestCase):
    races = {"NC-SEN": "U.S. Senate", "NC-01": "U.S. House · NC-01"}

    def setUp(self):
        self.contests = [
            result("NC-SEN", "Dem", "DEM", 30, 60, 9, 1),
            result("NC-SEN", "Rep", "REP", 40, 40, 5, 0),
            result("NC-SEN", "Lib", "LIB", 1, 2, 0, 0),
            result("NC-01", "Dem", "DEM", 10, 10, 0, 0),
            result("NC-01", "Rep", "REP", 10, 10, 0, 0),
        ]
        self.counties = [
            result("NC-SEN", "Dem", "DEM", 20, 50, 9, 1, "NEW HANOVER"),
            result("NC-SEN", "Dem", "DEM", 10, 10, 0, 0, "WAKE"),
            result("NC-SEN", "Rep", "REP", 25, 30, 5, 0, "NEW HANOVER"),
            result("NC-SEN", "Rep", "REP", 15, 10, 0, 0, "WAKE"),
            result("NC-SEN", "Lib", "LIB", 1, 2, 0, 0, "WAKE"),
            result("NC-01", "Dem", "DEM", 10, 10, 0, 0, "WAKE"),
            result("NC-01", "Rep", "REP", 10, 10, 0, 0, "WAKE"),
        ]
        self.meta = lambda pid, at: {
            "publication_id": pid,
            "source_id": "nc_test",
            "status": "unofficial",
            "published_at": at,
            "source_modified_at": at,
            "first_observed_at": at,
            "source_url": "https://example.com/results.zip",
            "raw_sha256": "a" * 64,
            "release_id": 1,
        }
        self.history = [{**r, "publication_id": "p2"} for r in self.contests] + [
            {**r, "publication_id": "p1", **dict.fromkeys(METHODS, 0)}
            | {"election_day": 5, "total_votes": 5}
            for r in self.contests
        ]
        self.demo = [
            ballots("EARLY VOTING", "DEM", "FEMALE", "WHITE", "UNDESIGNATED", 6),
            ballots("EARLY VOTING", "REP", "MALE", "BLACK", "UNDESIGNATED", 3),
            ballots("CIVILIAN", "UNA", "FEMALE", "ASIAN", "HISPANIC", 1),
            ballots("ONE-STOP", "DEM", "MALE", "WHITE", "UNDESIGNATED", 0),
        ]
        self.geo = json.loads((STATIC / "nc-map.json").read_text())
        self.names = {f["id"]: f["name"] for f in self.geo["counties"]}

    def build(self, **changes):
        arguments = dict(
            mode="published",
            races=self.races,
            results_metadata=self.meta("p2", "2026-11-04T02:00:00+00:00"),
            contest_rows=self.contests,
            county_rows=self.counties,
            history_points=[
                self.meta("p2", "2026-11-04T02:00:00+00:00"),
                self.meta("p1", "2026-11-04T01:00:00+00:00"),
            ],
            history_rows=self.history,
            history_truncated=False,
            demographics_metadata=self.meta("d1", "2026-11-04T03:00:00+00:00"),
            demographic_rows=self.demo,
            county_demographic_rows=[
                {"county": "WAKE", "request_type": "EARLY VOTING", "ballot_count": 9},
                {"county": "NEW HANOVER", "request_type": "CIVILIAN", "ballot_count": 1},
            ],
            county_names=self.names,
            party_group=party_group,
        )
        return build_analysis(**{**arguments, **changes})

    def test_known_race_totals_order_margin_and_method_mix(self):
        payload = self.build()
        senate, house = payload["results"]["races"]
        self.assertEqual([c["name"] for c in senate["candidates"]], ["Dem", "Rep", "Lib"])
        self.assertEqual(
            senate["totals"],
            dict(
                election_day=71,
                early_voting=102,
                absentee_by_mail=14,
                provisional=1,
                total_votes=188,
            ),
        )
        self.assertEqual(senate["margin"], 15)
        self.assertAlmostEqual(senate["margin_share"], 15 / 188)
        self.assertEqual(senate["leader"], "DEM")
        self.assertAlmostEqual(senate["candidates"][0]["share"], 100 / 188)
        # A tie has a zero margin and no leader, not a winner by sort order.
        self.assertEqual((house["margin"], house["leader"]), (0, None))
        names = [c["name"] for c in senate["counties"]]
        self.assertEqual(names, ["New Hanover", "Wake"])
        self.assertEqual(payload["mode"], "published")
        self.assertIn("not votes for any candidate", payload["notes"][1])

    def test_history_is_chronological_and_keeps_downward_corrections(self):
        points = self.build()["history"]["points"]
        self.assertEqual([p["publication_id"] for p in points], ["p1", "p2"])
        self.assertEqual(points[0]["races"]["NC-SEN"]["total"], 15)
        self.assertEqual(points[1]["races"]["NC-SEN"]["candidates"]["Rep"], 85)
        self.assertEqual(points[1]["races"]["NC-SEN"]["methods"]["early_voting"], 102)
        corrected = copy.deepcopy(self.history)
        for row in corrected:
            if row["publication_id"] == "p2" and row["choice"] == "Rep":
                row.update(election_day=row["election_day"] - 5)
                row["total_votes"] -= 5
        points = self.build(history_rows=corrected)["history"]["points"]
        self.assertEqual(points[1]["races"]["NC-SEN"]["candidates"]["Rep"], 80)

    def test_ballot_demographics_are_marginals_by_request_type(self):
        demo = self.build()["demographics"]
        self.assertEqual(demo["total"], 10)
        self.assertEqual(demo["request_types"], {"EARLY VOTING": 9, "CIVILIAN": 1, "ONE-STOP": 0})
        self.assertEqual(
            demo["dimensions"]["party_registration"]["EARLY VOTING"], {"DEM": 6, "REP": 3}
        )
        self.assertEqual(demo["dimensions"]["ethnicity"]["CIVILIAN"], {"HISPANIC": 1})
        self.assertEqual(demo["counties"][0]["name"], "New Hanover")
        missing = [{**self.demo[0], "party_registration": None}]
        self.assertEqual(
            self.build(demographic_rows=missing, county_demographic_rows=[])["demographics"][
                "dimensions"
            ]["party_registration"]["EARLY VOTING"],
            {"Not reported": 6},
        )

    def test_inconsistent_published_data_is_refused(self):
        bad_method = copy.deepcopy(self.contests)
        bad_method[0]["total_votes"] += 1
        duplicate = self.contests + [self.contests[0]]
        disagree = copy.deepcopy(self.counties)
        disagree[0].update(election_day=21, total_votes=81)
        unknown = self.counties + [result("NC-SEN", "Ghost", "", 1, 0, 0, 0, "WAKE")]
        negative = copy.deepcopy(self.contests)
        negative[0].update(provisional=-1, total_votes=98)
        cases = [
            dict(contest_rows=bad_method),
            dict(contest_rows=duplicate),
            dict(county_rows=disagree),
            dict(county_rows=unknown),
            dict(contest_rows=negative),
            dict(contest_rows=[r for r in self.contests if r["contest_id"] != "NC-01"]),
            dict(contest_rows=self.contests + [result("NC-02", "X", "DEM", 1, 0, 0, 0)]),
            dict(
                county_demographic_rows=[
                    {"county": "WAKE", "request_type": "EARLY VOTING", "ballot_count": 8}
                ]
            ),
        ]
        for case in cases:
            with self.subTest(case=list(case)), self.assertRaises(ValueError):
                self.build(**case)

    def test_missing_publications_stay_explicit(self):
        payload = self.build(
            results_metadata=None,
            contest_rows=[],
            county_rows=[],
            history_points=[],
            history_rows=[],
            demographics_metadata=None,
            demographic_rows=[],
            county_demographic_rows=[],
        )
        self.assertIsNone(payload["results"])
        self.assertIsNone(payload["history"])
        self.assertIsNone(payload["demographics"])

    def test_county_display_names_follow_the_map(self):
        self.assertEqual(display_name(self.names, "NEW HANOVER"), "New Hanover")
        self.assertEqual(display_name(self.names, "Unknown Place"), "Unknown Place")

    def test_demo_endpoint_is_labelled_and_cacheable(self):
        client = TestClient(create_app(demo=True))
        response = client.get("/api/analysis/nc")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "demo")
        self.assertEqual(payload["results"]["publication"]["status"], "test")
        self.assertEqual(len(payload["results"]["races"]), 15)
        self.assertEqual(len(payload["history"]["points"]), 3)
        again = client.get("/api/analysis/nc", headers={"If-None-Match": response.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        page = client.get("/analysis.html")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])

    def test_injected_non_database_provider_cannot_serve_analysis(self):
        client = TestClient(create_app(provider=lambda: {}, demo=False, state_providers={}))
        response = client.get("/api/analysis/nc")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")


@unittest.skipUnless(
    TestClient and os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable PostgreSQL *_test"
)
class AnalysisDatabaseTests(unittest.TestCase):
    """Published history and demographics read through the dashboard's reader role."""

    @classmethod
    def setUpClass(cls):
        from election_pipeline.database import connect, connection_settings

        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Analysis tests require a disposable *_test database")
        cls.root = Path(__file__).resolve().parents[1]
        cls.db = {**connection_settings(), "dbname": name}
        with connect(cls.db) as conn:
            for relation, filename in [
                ("ops.sources", "001_initial.sql"),
                ("serving.publications", "002_publication.sql"),
                ("ky.results", "003_state_schemas.sql"),
                ("tx.results", "004_mn_ne_tx.sql"),
            ]:
                if conn.execute("SELECT to_regclass(%s) AS t", (relation,)).fetchone()["t"] is None:
                    conn.execute((cls.root / "migrations" / filename).read_text())

    def setUp(self):
        import test_parsing as fixtures

        from election_pipeline.database import connect

        self.f = fixtures.ParsingTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        suffix = uuid.uuid4().hex
        self.f.source = replace(self.f.source, source_id="analysis_" + suffix)
        self.demographics = replace(
            self.f.source,
            source_id="analysis_demo_" + suffix,
            format="csv",
            expected_members=(),
            dataset="absentee_early_demographics",
            expected_columns=(
                "county_name",
                "abs_request_type",
                "party_cd",
                "gender",
                "race",
                "ethnicity",
                "0",
            ),
        )
        with connect(self.db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")

    def load(self, source, install):
        from election_pipeline.database import load_parsed
        from election_pipeline.parsing import parse_source

        self.f.source = source
        install()
        parsed = parse_source(source, self.f.settings, self.f.root)
        loaded = load_parsed(
            source, self.f.settings, self.f.root, Path(parsed["output_dir"]), self.db
        )
        self.assertEqual(loaded["outcome"], "succeeded")
        return loaded["release_id"]

    @contextmanager
    def reader(self):
        from election_pipeline.database import connect

        with connect(self.db) as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SET LOCAL ROLE election_dashboard")
            yield conn

    def test_reader_sees_published_history_and_demographics_only(self):
        from election_pipeline.publication import publish
        from election_pipeline.states import state_adapter

        results = self.f.source
        first = publish(self.root, self.db, self.load(results, self.f.archive), "test")
        corrected = [row.copy() for row in self.f.rows]
        corrected[0][9], corrected[0][13] = "1", "9"  # Downward Senate correction.
        second = publish(
            self.root,
            self.db,
            self.load(results, lambda: self.f.archive(corrected)),
            "test",
        )

        def install_demographics():
            path = self.f.root / "demographics.csv"
            path.write_text(
                "county_name,abs_request_type,party_cd,gender,race,ethnicity,0\n"
                "WAKE,EARLY VOTING,DEM,FEMALE,ASIAN,UNDESIGNATED,7\n"
                "NEW HANOVER,CIVILIAN,REP,MALE,WHITE,UNDESIGNATED,3\n",
                encoding="utf-8",
            )
            self.f.install(path)

        demo = publish(
            self.root, self.db, self.load(self.demographics, install_demographics), "test"
        )
        analysis = NCAnalysis(
            self.reader,
            results.source_id,
            state_adapter("NC").selected_races(results),
            {"WAKE": "Wake", "NEWHANOVER": "New Hanover"},
            party_group,
            demographics=self.demographics.source_id,
        )
        payload = analysis()
        self.assertEqual(
            payload["results"]["publication"]["publication_id"], second["publication_id"]
        )
        points = payload["history"]["points"]
        self.assertEqual(
            [p["publication_id"] for p in points],
            [first["publication_id"], second["publication_id"]],
        )
        self.assertEqual([p["races"]["NC-SEN"]["total"] for p in points], [20, 19])
        senate = payload["results"]["races"][0]
        self.assertEqual(senate["id"], "NC-SEN")
        self.assertEqual(senate["totals"]["total_votes"], 19)
        self.assertEqual(senate["counties"][0]["name"], "Wake")
        ballots = payload["demographics"]
        self.assertEqual(ballots["publication"]["publication_id"], demo["publication_id"])
        self.assertEqual(ballots["request_types"], {"EARLY VOTING": 7, "CIVILIAN": 3})
        self.assertEqual(
            {c["name"]: c["ballots"] for c in ballots["counties"]},
            {"New Hanover": {"CIVILIAN": 3}, "Wake": {"EARLY VOTING": 7}},
        )
        # The same pinned publications are served from cache without another query.
        self.assertIs(analysis(), payload)
        with self.reader() as conn:
            self.assertFalse(
                conn.execute(
                    "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                ).fetchone()["ok"]
            )


if __name__ == "__main__":
    unittest.main()
