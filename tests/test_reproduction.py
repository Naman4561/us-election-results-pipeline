"""A live publication re-created from its raw snapshot: exact, or reported as different."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loading import migrated, requires_database
from snapshots import SNAPSHOTS
from test_alaska import AlaskaTests

from election_pipeline import publication
from election_pipeline.database import connect, load_parsed
from election_pipeline.deployment import catalog, control
from election_pipeline.parsing import parse_source
from election_pipeline.reproduction import compared, referenced_members, reproduce, scratch_copy
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]


class ComparisonTests(unittest.TestCase):
    def test_rows_compare_as_multisets(self):
        from collections import Counter

        same = Counter({("AK-AL", "Begich, Nick", 1): 1})
        self.assertIsNone(compared(same, Counter(same)))
        found = compared(same, Counter({("AK-AL", "Begich, Nick", 2): 1}))
        self.assertEqual((found["published_only"], found["rebuilt_only"]), (1, 1))
        self.assertEqual(found["published_only_sample"], [["AK-AL", "Begich, Nick", "1"]])
        # A duplicated row is a difference too.
        self.assertEqual(compared(same, same + same)["rebuilt_only"], 1)

    def test_a_scratch_store_holds_the_county_reports_a_bundle_keeps_outside_itself(self):
        # Mississippi's bundle names each county report by SHA-256 in the member store.
        settings, sources = load_config(ROOT / "config")
        source = sources["ms_2026_general_all"]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            sha = SNAPSHOTS[source.source_id](Path(first), source, settings)
            raw = ArchiveStore(Path(first), source).archive_path(sha)
            kept = {p.stem for p in (raw.parent / "members").glob("*.bin")}
            self.assertGreater(len(kept), 0)
            self.assertLessEqual(kept, referenced_members(raw))
            scratch = ArchiveStore(Path(second), source)
            scratch_copy(raw, scratch, sha)
            again = parse_source(source, settings, Path(second), sha256=sha)
            self.assertEqual(again["outcome"], "parsed", again)
            original = parse_source(source, settings, Path(first), sha256=sha)
            self.assertEqual(again["output_sha256"], original["output_sha256"])
            # Without them, the parse is refused (the failure the first nightly run found).
            for path in (scratch.archive_path(sha).parent / "members").glob("*.bin"):
                path.unlink()
            shutil.rmtree(Path(second) / "parsed")
            refused = parse_source(source, settings, Path(second), sha256=sha)
            self.assertIn("missing from the member store", refused["error"])


@requires_database
class ReproductionTests(unittest.TestCase):
    def test_a_publication_reproduces_and_a_changed_one_is_reported(self):
        db = migrated()
        with connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        fixture = AlaskaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        report = fixture.parse()
        loaded = load_parsed(
            fixture.source, fixture.settings, fixture.root, Path(report["output_dir"]), db
        )
        prepared = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        published = publication.publish_prepared(ROOT, db, prepared["candidate_id"])
        identity = published["publication_id"]

        def run():
            return reproduce(ROOT, fixture.source, fixture.settings, fixture.root, db, identity)

        result = run()
        self.assertEqual(result["outcome"], "reproduced", result)
        self.assertEqual(
            set(result["tables"]),
            {"serving.contest_results", "serving.county_results", "serving.rcv_rounds"},
        )
        self.assertEqual(set(result["tables"].values()), {"identical"})
        self.assertEqual(result["published_transform_sha256"], result["current_transform_sha256"])
        with connect(db) as conn:
            # No candidate schema is left behind.
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name LIKE 'r\\_ak\\_%'"
                ).fetchall()
            )
            conn.execute(
                "UPDATE serving.rcv_rounds SET votes = votes + 1 WHERE publication_id = %s "
                "AND outcome = 'elected'",
                (identity,),
            )
            conn.execute(
                "UPDATE ops.processing_releases SET parser_report = jsonb_set(parser_report, "
                "'{output_sha256,contest_results.csv}', to_jsonb(repeat('0', 64))) "
                "WHERE release_id = %s",
                (loaded["release_id"],),
            )
        result = run()
        self.assertEqual(result["outcome"], "differs", result)
        self.assertEqual(result["differences"]["parsed_files"], ["contest_results.csv"])
        rounds = result["differences"]["tables"]["serving.rcv_rounds"]
        self.assertEqual((rounds["published_only"], rounds["rebuilt_only"]), (1, 1))
        self.assertEqual(result["tables"]["serving.contest_results"], "identical")


@requires_database
class CrossCheckTests(unittest.TestCase):
    def test_a_candidate_whose_dbt_totals_differ_from_the_parser_is_refused(self):
        db = migrated()
        with connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        fixture = AlaskaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        report = fixture.parse()
        loaded = load_parsed(
            fixture.source, fixture.settings, fixture.root, Path(report["output_dir"]), db
        )
        # What dbt derives from the landing rows no longer matches what the parser wrote.
        with connect(db) as conn:
            conn.execute(
                "UPDATE landing.results SET total_votes = total_votes + 1 "
                "WHERE release_id = %s AND unit_type = 'state' AND contest_id = 'AK-SEN' "
                "AND choice = 'Peltola, Mary'",
                (loaded["release_id"],),
            )
        with self.assertRaisesRegex(ValueError, "dbt contest_results differ from the parser"):
            publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
        with connect(db) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM serving.current_publications WHERE source_id = %s",
                    (fixture.source.source_id,),
                ).fetchone()
            )


class HostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"CONTROL_STATE_DIR": str(self.root), "JOB_CPUS": "1", "JOB_MEMORY": "1g"}
        self.value = {
            "release": {
                "revision": "d" * 40,
                "image_id": "sha256:" + "c" * 64,
                "release_path": "/r",
            }
        }
        self.live = {"ak_2026_general": ("p1", "a" * 64), "nc_2026_general_all": ("p2", "b" * 64)}
        self.configured = set(self.live)
        self.jobs = []

    def run_reproduce(self, outcomes, names=None, keys=None, check_all=False):
        def job(image, prod, config, args, timeout):
            name = args[args.index("--source") + 1]
            self.jobs.append(name)
            if outcomes[name] == "crash":
                raise RuntimeError("Command failed: writer-secret")
            return json.dumps({"source_id": name, "outcome": outcomes[name]})

        with (
            patch.object(catalog, "read", return_value=self.value),
            patch.object(control, "live_results", return_value=dict(self.live)),
            patch.object(control, "configured_sources", return_value=set(self.configured)),
            patch.object(control, "check_keys", return_value=dict(keys or {})),
            patch.object(control.containers, "run_job", side_effect=job),
        ):
            self.jobs.clear()
            return control.reproduce(
                "catalog", {"POSTGRES_PASSWORD": "writer-secret"}, self.config, names, check_all
            )

    def latest(self):
        path = self.root / "reproduction" / "latest.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_reproduced_sources_are_reused_only_while_publication_and_code_are_unchanged(self):
        keys = {"ak_2026_general": "k1", "nc_2026_general_all": "k2"}
        both = {"ak_2026_general": "reproduced", "nc_2026_general_all": "differs"}
        summary = self.run_reproduce(both, keys=keys)
        self.assertEqual(summary["outcome"], "differs")
        self.assertEqual(summary["counts"], {"differs": 1, "reproduced": 1})
        self.assertEqual(self.latest()["sources"]["ak_2026_general"]["key"], "k1")
        # Alaska reproduced under the same code and publication: reused. North Carolina
        # differed, so it runs again.
        self.run_reproduce(both, keys=keys)
        self.assertEqual(self.jobs, ["nc_2026_general_all"])
        self.assertTrue(self.latest()["sources"]["ak_2026_general"]["reused"])
        # A new publication, new code, or --check-all runs it again.
        self.live["ak_2026_general"] = ("p3", "e" * 64)
        self.run_reproduce(both, keys=keys)
        self.assertIn("ak_2026_general", self.jobs)
        self.run_reproduce(both, keys={**keys, "ak_2026_general": "k9"})
        self.assertIn("ak_2026_general", self.jobs)
        self.run_reproduce(both, keys={**keys, "ak_2026_general": "k9"}, check_all=True)
        self.assertEqual(sorted(self.jobs), ["ak_2026_general", "nc_2026_general_all"])
        # Without keys nothing is reused.
        self.run_reproduce(both)
        self.assertEqual(len(self.jobs), 2)

    def test_a_source_the_release_no_longer_configures_is_listed_not_run(self):
        # North Carolina's old source, which nc_2026_general_all replaced, still has a
        # current publication in serving.
        self.live["nc_2026_general"] = ("p0", "f" * 64)
        summary = self.run_reproduce(
            {"ak_2026_general": "reproduced", "nc_2026_general_all": "reproduced"}
        )
        self.assertEqual(sorted(self.jobs), ["ak_2026_general", "nc_2026_general_all"])
        self.assertEqual(summary["outcome"], "reproduced")
        self.assertEqual(summary["not_configured"], ["nc_2026_general"])
        self.assertNotIn("nc_2026_general", summary["sources"])
        with self.assertRaisesRegex(ValueError, "No live results publication"):
            self.run_reproduce({}, names=["nc_2026_general"])

    def test_a_failed_job_is_reported_and_a_partial_run_keeps_latest(self):
        summary = self.run_reproduce(
            {"ak_2026_general": "reproduced", "nc_2026_general_all": "crash"}
        )
        failed = summary["sources"]["nc_2026_general_all"]
        self.assertEqual(failed["outcome"], "failed")
        self.assertNotIn("writer-secret", failed["error"])
        before = self.latest()
        self.run_reproduce({"ak_2026_general": "differs"}, names=["ak_2026_general"])
        self.assertEqual(self.latest(), before)
        self.assertEqual(len(list((self.root / "reproduction").glob("reproduction-*.json"))), 2)
        with self.assertRaisesRegex(ValueError, "No live results publication"):
            self.run_reproduce({}, names=["zz_2026_general"])
