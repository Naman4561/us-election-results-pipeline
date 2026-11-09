"""The one-time copy of per-state tables into the shared ones (migration 024)."""

import os
import unittest
import uuid

from psycopg import sql

from election_pipeline.consolidate import consolidate
from election_pipeline.database import connect, connection_settings
from election_pipeline.migrations import migrate

SHA = "a" * 64


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        admin = {**connection_settings(), "dbname": "postgres"}
        name = "consolidate_" + uuid.uuid4().hex[:12] + "_test"
        with connect(admin) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        def drop():
            with connect(admin) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

        self.addCleanup(drop)
        self.db = {**connection_settings(), "dbname": name}
        with connect(self.db) as conn:
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='election_dashboard'"
            ).fetchone():
                conn.execute("CREATE ROLE election_dashboard NOLOGIN")
            migrate(conn, through="023_wv.sql")
            self.releases = {
                state: self.release(conn, state, fmt)
                for state, fmt in (("IA", "bundle"), ("NC", "zip"), ("KY", "bundle"))
            }
            self.old_rows(conn)

    def release(self, conn, state, fmt):
        source = state.lower() + "_2026_general"
        conn.execute(
            "INSERT INTO ops.sources (source_id, state, election_date, dataset, source_url, format)"
            " VALUES (%s, %s, '2026-11-03', 'election_results', 'https://example.com', %s)",
            (source, state, fmt),
        )
        snapshot = conn.execute(
            "INSERT INTO ops.snapshots (source_id, raw_sha256, raw_path) VALUES (%s, %s, 'raw')"
            " RETURNING snapshot_id",
            (source, SHA),
        ).fetchone()["snapshot_id"]
        return conn.execute(
            "INSERT INTO ops.processing_releases (snapshot_id, parser_fingerprint,"
            " load_contract_version, report_path, parser_report, expected_rows, loaded_rows,"
            " loaded_at) VALUES (%s, %s, 1, 'r', '{}', 2, 2, now()) RETURNING release_id",
            (snapshot, SHA),
        ).fetchone()["release_id"]

    def old_rows(self, conn):
        ia, nc, ky = (self.releases[s] for s in ("IA", "NC", "KY"))
        conn.execute(
            "INSERT INTO ia.results (release_id, source_line, unit_type, county, contest_id,"
            " source_contest_id, source_candidate_id, choice, choice_party, total_votes,"
            " election_day, absentee_combined, precincts_reporting, precincts_total) VALUES"
            " (%s, 2, 'state', '', 'IA-SEN', '1', 'a', 'A', 'DEM', 7, 3, 4, NULL, NULL),"
            " (%s, 3, 'county', 'POLK', 'IA-SEN', '1', 'a', 'A', 'DEM', 7, 3, 4, 1, 2)",
            (ia, ia),
        )
        conn.execute(
            "INSERT INTO nc.results (release_id, source_line, county, precinct, contest_id,"
            " contest_name, source_contest_group_id, contest_type, choice, choice_party,"
            " vote_for, real_precinct, election_day, early_voting, absentee_by_mail,"
            " provisional, total_votes) VALUES"
            " (%s, 2, 'WAKE', '001', 'NC-SEN', 'US SENATE', '1383', 'S', 'A', 'DEM', 1, true,"
            " 1, 2, 3, 4, 10),"
            " (%s, 3, 'WAKE', 'ABSENTEE', 'NC-SEN', 'US SENATE', '1383', 'S', 'A', 'DEM', 1,"
            " false, 0, 1, 0, 0, 1)",
            (nc, nc),
        )
        conn.execute(
            "INSERT INTO ky.results (release_id, source_line, reporting_unit_id, unit_type,"
            " county, precinct, contest_id, choice, choice_party, election_day,"
            " absentee_combined, total_votes) VALUES"
            " (%s, 2, 'S', 'state', '', '', 'KY-SEN', 'A', 'REP', 5, 1, 6),"
            " (%s, 3, 'C1', 'county', 'ADAIR', '', 'KY-SEN', 'A', 'REP', 5, 1, 6)",
            (ky, ky),
        )
        conn.execute(
            "INSERT INTO ky.reporting_units (release_id, source_line, reporting_unit_id,"
            " unit_type, county, precinct, reporting_status, precincts_participating,"
            " precincts_reporting, ballots_cast, registered_voters) VALUES"
            " (%s, 2, 'C1', 'county', 'ADAIR', '', 'FinalReporting', 3, 3, 9, 12)",
            (ky,),
        )
        attempt = uuid.uuid4()
        conn.execute(
            "INSERT INTO ops.publication_attempts (attempt_id, release_id, action, outcome,"
            " finished_at) VALUES (%s, %s, 'publish', 'published', now())",
            (attempt, ia),
        )
        conn.execute(
            "INSERT INTO serving.publications (publication_id, source_id, release_id,"
            " snapshot_id, state, election_date, dataset, source_url, raw_sha256,"
            " transform_sha256, status) SELECT %s, s.source_id, r.release_id, r.snapshot_id,"
            " 'IA', '2026-11-03', 'election_results', 'https://example.com', %s, %s,"
            " 'unofficial' FROM ops.processing_releases r JOIN ops.snapshots s USING"
            " (snapshot_id) WHERE r.release_id = %s",
            (attempt, SHA, SHA, ia),
        )
        conn.execute(
            "INSERT INTO ia.contest_candidate_results VALUES (%s, 'IA-SEN', 'A', 'DEM', 7, 3, 4,"
            " 7, 1)",
            (attempt,),
        )
        conn.execute(
            "INSERT INTO ia.county_candidate_results VALUES (%s, 'POLK', 'IA-SEN', 'A', 'DEM',"
            " 7, 3, 4)",
            (attempt,),
        )
        self.publication = attempt

    def test_rows_methods_units_and_publications_are_copied_once_and_checked(self):
        with connect(self.db) as conn:
            migrate(conn)
            report = consolidate(conn)
            self.assertEqual(report["ia"], "copied")
            self.assertEqual(report["nc"], "copied")
            self.assertEqual(report["ky"], "copied")
            rows = conn.execute(
                "SELECT state, unit_type, county, precinct, source_contest_id,"
                " source_candidate_id, attributes FROM landing.results ORDER BY state, source_line"
            ).fetchall()
            nc = [r for r in rows if r["state"] == "NC"]
            self.assertEqual([r["unit_type"] for r in nc], ["precinct", "precinct"])
            self.assertEqual(nc[1]["precinct"], "ABSENTEE")
            self.assertEqual(nc[0]["source_contest_id"], "1383")
            self.assertEqual(nc[0]["attributes"]["contest_name"], "US SENATE")
            self.assertIs(nc[1]["attributes"]["real_precinct"], False)
            ky = [r for r in rows if r["state"] == "KY"]
            self.assertEqual(ky[1]["attributes"], {"reporting_unit_id": "C1"})
            self.assertEqual(ky[1]["source_candidate_id"], "A")
            methods = conn.execute(
                "SELECT method, sum(votes)::int AS votes FROM landing.result_methods m"
                " JOIN landing.results r USING (release_id, source_line)"
                " WHERE r.state = 'NC' GROUP BY method"
            ).fetchall()
            self.assertEqual(
                {m["method"]: m["votes"] for m in methods},
                {"election_day": 1, "early_voting": 3, "absentee_by_mail": 3, "provisional": 4},
            )
            unit = conn.execute("SELECT * FROM landing.reporting_units").fetchone()
            self.assertEqual((unit["state"], unit["source_unit_id"]), ("KY", "C1"))
            contest = conn.execute("SELECT * FROM serving.contest_results").fetchone()
            self.assertEqual((contest["state"], contest["total_votes"]), ("IA", 7))
            self.assertEqual(
                conn.execute("SELECT count(*) AS n FROM serving.county_method_results").fetchone()[
                    "n"
                ],
                2,
            )
            # A rerun only checks, and new shared rows for the same state do not count.
            self.assertEqual(consolidate(conn)["ia"], "verified")
            conn.execute(
                "UPDATE landing.result_methods SET votes = votes + 1"
                " WHERE release_id = %s AND method = 'mail'",
                (self.releases["IA"],),
            )
            conn.execute(
                "UPDATE landing.results SET total_votes = 8 WHERE release_id = %s"
                " AND source_line = 2",
                (self.releases["IA"],),
            )
            with self.assertRaisesRegex(ValueError, "ia: landing rows differ"):
                consolidate(conn, ("ia",))
            # The originals are untouched.
            self.assertEqual(
                conn.execute("SELECT sum(total_votes)::int AS n FROM ia.results").fetchone()["n"],
                14,
            )


if __name__ == "__main__":
    unittest.main()
