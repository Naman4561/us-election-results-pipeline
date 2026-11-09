"""Known-answer result semantics and HTTP publication consistency checks."""

import copy
import json
import unittest
from unittest.mock import MagicMock, patch

try:
    from fastapi.testclient import TestClient

    from election_pipeline.dashboard.server import (
        STATIC,
        PublishedResults,
        create_app,
        demo_payload,
        make_payload,
        national_payload,
        party_group,
        summarize,
    )
except ImportError:
    TestClient = None

from election_pipeline.states import state_adapter

RACES = state_adapter("NC").races
METHODS = state_adapter("NC").methods[1:]


@unittest.skipUnless(TestClient, "Install postgres, dashboard extras and httpx")
class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geo = json.loads((STATIC / "nc-map.json").read_text())

    def row(self, name="A", party="DEM", votes=10):
        return {
            "county": "Wake",
            "contest_id": "NC-SEN",
            "choice": name,
            "choice_party": party,
            "total_votes": votes,
            "election_day": votes,
            "early_voting": 0,
            "absentee_by_mail": 0,
            "provisional": 0,
        }

    def test_zero_tie_other_unknown_and_method_specific_lead(self):
        self.assertEqual(summarize([self.row(votes=0)], "total_votes")["status"], "unreported")
        rows = [self.row(), self.row("B", "REP")]
        self.assertEqual(summarize(rows, "total_votes")["status"], "tie")
        self.assertIsNone(summarize(rows, "total_votes")["leader"])
        self.assertEqual(party_group("UNA", "Independent"), "OTHER")
        self.assertEqual(party_group(None, "Write-In"), "OTHER")
        self.assertEqual(party_group(None, "Unknown candidate"), "UNKNOWN")
        rows[1].update(total_votes=20, election_day=0, early_voting=20)
        self.assertEqual(summarize(rows, "total_votes")["leader"], "REP")
        self.assertEqual(summarize(rows, "election_day")["leader"], "DEM")

    def test_vote_arithmetic_geography_and_reconciliation_are_enforced(self):
        row = self.row()
        complete = [{**row, "contest_id": identity} for identity in RACES]
        payload = make_payload(self.geo, {"publication_id": "one"}, complete, complete)
        self.assertEqual(
            payload["races"]["NC-SEN"]["counties"]["WAKE"]["stats"]["total_votes"]["total"], 10
        )
        for bad in ({**row, "county": "Unknown county"}, {**row, "election_day": 9}):
            with self.assertRaises(ValueError):
                make_payload(self.geo, {}, [bad], [row])
        with self.assertRaises(ValueError):
            make_payload(
                self.geo, {}, complete, [{**complete[0], "total_votes": 11}, *complete[1:]]
            )
        with self.assertRaisesRegex(ValueError, "every race"):
            make_payload(self.geo, {}, complete[:-1], complete[:-1])

    def test_http_etag_changes_with_publication_and_restore(self):
        first = demo_payload(self.geo)
        second = copy.deepcopy(first)
        second["publication"]["publication_id"] = "second"
        current = [first]
        with TestClient(create_app(state_providers={"NC": lambda: current[0]})) as client:
            one = client.get("/api/states/NC")
            self.assertEqual(one.status_code, 200)
            tag = one.headers["etag"]
            self.assertEqual(
                client.get("/api/states/NC", headers={"If-None-Match": tag}).status_code, 304
            )
            current[0] = second
            two = client.get("/api/states/NC", headers={"If-None-Match": tag})
            self.assertEqual(two.status_code, 200)
            self.assertEqual(two.json()["publication"]["publication_id"], "second")
            current[0] = first
            restored = client.get("/api/states/NC", headers={"If-None-Match": two.headers["etag"]})
            self.assertEqual(restored.json()["publication"]["publication_id"], "demo-v1")
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/app.js").status_code, 200)

    def test_failure_is_explicit_and_never_substitutes_demo_data(self):
        def fail():
            raise ValueError("private-password-must-not-leak")

        with TestClient(create_app(state_providers={"NC": fail})) as client:
            response = client.get("/api/states/NC")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-password", response.text)
            self.assertNotIn("demo", response.text)

    def test_reader_pins_the_publication_for_both_queries_and_cache(self):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.transaction.return_value.__enter__.return_value = None
        metadata = {"publication_id": "old", "release_id": 1, "status": "test"}
        row = self.row()
        # Role check, current publication, then rows and method rows for counties and contests.
        responses = [MagicMock() for _ in range(7)]
        responses[1].fetchone.return_value = {
            "rolsuper": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
        }
        responses[2].fetchone.return_value = metadata
        totals = {k: v for k, v in row.items() if k not in METHODS}
        for index, place in ((3, {"county": row["county"]}), (5, {})):
            responses[index].fetchall.return_value = [
                {**totals, **place, "contest_id": identity} for identity in RACES
            ]
            responses[index + 1].__iter__.return_value = iter(
                [
                    {
                        **place,
                        "contest_id": identity,
                        "choice": row["choice"],
                        "method": m,
                        "votes": row[m],
                    }
                    for identity in RACES
                    for m in METHODS
                ]
            )
        conn.execute.side_effect = responses
        with patch("election_pipeline.dashboard.server.psycopg.connect", return_value=conn):
            result = PublishedResults({}, self.geo, "nc_2026_general_all")()
        self.assertEqual(result["publication"]["publication_id"], "old")
        for call in conn.execute.call_args_list[-4:]:
            self.assertEqual(call.args[1], ("old",))

    def test_reader_reuses_a_recent_payload_without_reconnecting(self):
        reader = PublishedResults({}, self.geo, "nc_2026_general_all")
        with patch.object(reader, "read", return_value={"races": {}}) as read:
            first, second = reader(), reader()
            self.assertIs(first, second)
            self.assertEqual(read.call_count, 1)
            reader.recent = (reader.recent[0] - reader.TTL_SECONDS - 1, reader.recent[1])
            reader()
            self.assertEqual(read.call_count, 2)

    def test_national_summary_omits_county_detail_served_per_state(self):
        full = demo_payload(self.geo)
        self.assertTrue(any(c.get("counties") for c in full["races"].values()))
        national = national_payload({"NC": lambda: full})["states"]["NC"]
        for race, contest in national["races"].items():
            self.assertNotIn("counties", contest)
            self.assertEqual(contest["stats"], full["races"][race]["stats"])
        self.assertNotIn("county_feeds", national)
        with TestClient(create_app(demo=True)) as client:
            whole = client.get("/api/national")
            state = client.get("/api/states/NC").json()
            self.assertTrue(any(c.get("counties") for c in state["races"].values()))
            national_ms = whole.json()["states"]["MS"]
            detail_ms = client.get("/api/states/MS").json()
            self.assertTrue(national_ms["county_feeds"])
            self.assertEqual(national_ms["county_feeds"], detail_ms["county_feeds"])
            self.assertNotIn("counties", national_ms["races"]["MS-SEN"])
            self.assertLess(len(whole.content), 1_000_000)
            self.assertEqual(client.get("/nc-map.json").headers["cache-control"], "no-cache")
            self.assertIn("no-cache", whole.headers["cache-control"])

    def test_boundary_coverage_and_demo_categories(self):
        self.assertEqual(len(self.geo["counties"]), 100)
        self.assertEqual(
            {f["id"] for f in self.geo["districts"]}, {f"NC-{i:02}" for i in range(1, 15)}
        )
        counties = demo_payload(self.geo)["races"]["NC-SEN"]["counties"]
        self.assertEqual(counties["HYDE"]["stats"]["total_votes"]["leader"], "OTHER")
        self.assertEqual(counties["DARE"]["stats"]["total_votes"]["status"], "tie")
