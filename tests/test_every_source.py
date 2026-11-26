"""Every configured source passes the same generic checks, from its own fixture.

``snapshots.SNAPSHOTS`` stores each source's captured (or, for four, synthetic) fixture
as production would. From it, every source must parse, verify, convert and replay; parse
the same beside an earlier snapshot with higher counts (a downward correction is just
another snapshot); draw on its state's map and dashboard; and load, validate and publish
to a database the dashboard may read. A state's own test file keeps what is specific to
its format: the refusals of an edited capture, and the figures it reports.
"""

import contextlib
import csv
import dataclasses
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from loading import copies, migrated, requires_database
from snapshots import SNAPSHOTS, SYNTHETIC

from election_pipeline import parse_report, publication
from election_pipeline.counties import key as county_key
from election_pipeline.dashboard.server import STATIC, make_payload, national_payload
from election_pipeline.database import connect, load_parsed
from election_pipeline.parsers.scope import recording
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import STATES, state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
SETTINGS, SOURCES = load_config(ROOT / "config")
# The SHA-256 of every file each fixture parses to, from before the parsers read the election
# definition (tests/fixtures/registry/provenance.json). Mississippi's county feed status
# records the time of collection and is left out, as the nightly reproduction leaves it out.
GOLDEN_OUTPUTS = json.loads(
    (ROOT / "tests/fixtures/registry/parse_outputs_2026.json").read_text(encoding="utf-8")
)
TIMED = {("ms_2026_general_all", "county_feeds.csv")}
# The same files with the county_fips column (step 3.5): pinned byte for byte, and, with that
# column dropped again, the old hashes above: nothing but the new column changed.
GOLDEN_FIPS = json.loads(
    (ROOT / "tests/fixtures/registry/parse_outputs_2026_fips.json").read_text(encoding="utf-8")
)
WITH_FIPS = ("results.csv", "county_results.csv")


def without_fips(directory, name):
    """SHA-256 of an output file as it was before it had a county_fips column."""
    with (Path(directory) / name).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    column = rows[0].index("county_fips")
    out = io.StringIO(newline="")
    csv.writer(out).writerows([r[:column] + r[column + 1 :] for r in rows])
    return hashlib.sha256(out.getvalue().encode("utf-8")).hexdigest()


def legacy_hashes(report):
    return {
        name: without_fips(report["output_dir"], name) if name in WITH_FIPS else digest
        for name, digest in report["output_sha256"].items()
        if (report["source_id"], name) not in TIMED
    }


def check_fips(report, state):
    """Each county row's FIPS is the FIPS of the map's feature the dashboard would join it to."""
    geography = json.loads((STATIC / f"{state.lower()}-map.json").read_text(encoding="utf-8"))[
        "counties"
    ]
    fips = {f["id"]: f["fips"] for f in geography}
    seen = set()
    for name in WITH_FIPS:
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["county"]:
                    assert row["county_fips"] == fips[county_key(row["county"])], row
                    seen.add(row["county_fips"])
                else:
                    assert row["county_fips"] == "", row
    return seen


# What each fixture's report reads as (official status, report time and its basis,
# reconciliations): the list of sources whose time has no known zone, or none, is what the
# rule "newer means a later report time" must decide about (plan step 3.7).
GOLDEN_READINGS = json.loads(
    (ROOT / "tests/fixtures/registry/readings_2026.json").read_text(encoding="utf-8")
)
# Rows and contests each fixture parses to. Where a batch test asserted them before the
# registry existed (GA, SC, KS, OH and the nine Enhanced Voting, Clarity and state-site
# captures), these are its figures.
PARSED = {
    "ak_2026_general": (492, 3),
    "al_2026_general": (648, 9),
    "ar_2026_general": (659, 6),
    "az_2026_general": (281, 10),
    "ca_2026_general": (106, 53),
    "co_2026_general": (1201, 10),
    "ct_2026_general": (1101, 6),
    "de_2026_general": (16, 2),
    "fl_2026_general": (1248, 29),
    "ga_2026_general": (1030, 16),
    "hi_2026_general": (8, 3),
    "ia_2026_general": (1076, 6),
    "id_2026_general": (710, 4),
    "il_2026_general": (94, 10),
    "in_2026_general": (309, 9),
    "ks_2026_general": (522, 6),
    "ky_2026_general": (1025, 7),
    "la_2026_general": (718, 7),
    "ma_2026_general": (4232, 11),
    "md_2026_general": (282, 9),
    "me_2026_general": (169, 4),
    "mi_2026_general": (1635, 15),
    "mn_2026_general_all": (941, 10),
    "mo_2026_general": (28, 8),
    "ms_2026_general_all": (39, 1),
    "mt_2026_general": (447, 3),
    "nc_2026_general_all": (31, 15),
    "nd_2026_general": (270, 1),
    "ne_2026_general_all": (15, 5),
    "nh_2026_general": (149, 4),
    "nj_2026_general": (321, 13),
    "nm_2026_general": (226, 5),
    "nv_2026_general": (157, 5),
    "ny_2026_general": (567, 27),
    "oh_2026_general": (1475, 17),
    "ok_2026_general": (992, 7),
    "or_2026_general": (510, 8),
    "pa_2026_general": (369, 18),
    "ri_2026_general": (17, 4),
    "sc_2026_general": (687, 9),
    "sd_2026_general": (402, 3),
    "tn_2026_general": (71, 11),
    "tx_2026_general_all": (93, 40),
    "ut_2026_general": (192, 4),
    "va_2026_general": (1010, 12),
    "vt_2026_general": (3224, 2),
    "wa_2026_general": (180, 10),
    "wi_2026_general": (60, 5),
    "wv_2026_general": (311, 3),
    "wy_2026_general": (288, 3),
}
# Counties a district race is counted in, where a district crosses county lines.
COUNTY_COUNTS = {
    "co_2026_general": {"CO-05": 1},
    "ia_2026_general": {"IA-04": 36},
    "id_2026_general": {"ID-01": 19},
    "md_2026_general": {"MD-01": 11},
    "or_2026_general": {"OR-02": 22},
    "va_2026_general": {"VA-11": 2},
    "wa_2026_general": {"WA-03": 7, "WA-07": 1},
    "wv_2026_general": {"WV-01": 28, "WV-02": 27},
}
# Fixtures that captured some of a state's House races: the rest were not yet posted
# (Wisconsin's and Illinois's county pages) or not yet collected (Mississippi's counties).
SOME_HOUSE_RACES = {"il_2026_general", "ms_2026_general_all", "wi_2026_general"}
# Statewide races not counted in every place the map draws: the counties a fixture
# captured (Illinois's and Wisconsin's county pages, Mississippi's 13 county reports),
# counties that had not reported (Kansas), Vermont's unorganized towns and gores, which
# have no voters, and the synthetic exports.
SOME_COUNTIES = {
    "il_2026_general",
    "ks_2026_general",
    "ms_2026_general_all",
    "vt_2026_general",
    "wi_2026_general",
    *SYNTHETIC,
}
CONSISTENT_MODES = {
    ("none", "not_available"),
    ("exact", "passed"),
    ("exact", "not_available"),
    ("masked", "not_available"),
    ("masked", "passed_except_privacy_masked_counties"),
}
RECONCILED = {"passed", "passed_except_privacy_masked_counties", "not_available"}


def parsed(source_id, root):
    """The source's snapshot stored in ``root`` and parsed."""
    source = SOURCES[source_id]
    synthetic = recording() if source_id in SYNTHETIC else contextlib.nullcontext()
    with synthetic:
        sha = SNAPSHOTS[source_id](root, source, SETTINGS)
        return sha, parse_source(source, SETTINGS, root, sha256=sha)


def counted(report, name, methods):
    with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
        found = list(csv.DictReader(handle))
    for row in found:
        for field in methods:
            row[field] = int(row[field]) if row[field] else None
    return found


def feeds(report):
    """Each county's collection status, as the dashboard reads it."""
    found = counted(report, "county_feeds.csv", ())
    for row in found:
        row.update({k: v or None for k, v in row.items()})
        for name in ("precincts_reporting", "precincts_total", "write_in_votes"):
            row[name] = int(row[name]) if row[name] is not None else None
    return found


def inflated(directory):
    """Every count in a parse's CSVs ten times over: an earlier, higher snapshot."""
    for path in directory.glob("*.csv"):
        with path.open(encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        for row in table[1:]:
            row[:] = [str(int(v) * 10) if v.isdigit() else v for v in row]
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(table)


class EverySourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_every_results_source_has_a_snapshot_and_its_figures(self):
        results = {state.source_id for state in STATES.values()}
        self.assertEqual(set(SNAPSHOTS), results)
        self.assertEqual(set(PARSED), results)
        # Configured sources that are not a state's election results.
        self.assertEqual(
            {s.dataset for k, s in SOURCES.items() if k not in results},
            {"absentee_early_demographics"},
        )
        self.assertLessEqual(SYNTHETIC, results)

    def test_every_snapshot_parses_verifies_converts_replays_and_draws(self):
        providers = {}
        for source_id in sorted(SNAPSHOTS):
            source = SOURCES[source_id]
            adapter = state_adapter(source.state)
            with self.subTest(source_id):
                root = self.root / source_id
                sha, report = parsed(source_id, root)
                self.assertEqual(report["outcome"], "parsed", report)
                self.assertEqual(
                    (report["output_rows"], report["contest_count"]), PARSED[source_id]
                )
                self.assertEqual(
                    {
                        name: digest
                        for name, digest in report["output_sha256"].items()
                        if (source_id, name) not in TIMED
                    },
                    GOLDEN_FIPS[source_id],
                )
                self.assertEqual(legacy_hashes(report), GOLDEN_OUTPUTS[source_id])
                places = check_fips(report, source.state)
                self.assertEqual(bool(places), adapter.county_mode != "none")
                self.assertEqual(
                    dataclasses.asdict(parse_report.read(report)), GOLDEN_READINGS[source_id]
                )
                # One setting, two checks: dbt's mode (from the registry) and the parser's
                # outcome must agree. Where a state's statewide totals are the sum of its
                # counties (six exact states), the parser has nothing to compare and says so.
                self.assertIn(
                    (adapter.county_mode, report["county_reconciliation"]), CONSISTENT_MODES
                )
                # Every report states its source's official status; none is left to a default
                # that reads as "not official".
                self.assertIn(report["official_status"], parse_report.OFFICIAL_STATUSES)
                self.assertEqual(
                    report["official_results"], report["official_status"] == parse_report.OFFICIAL
                )
                self.assertFalse(report["all_counts_zero"])
                self.assertIn(report["county_reconciliation"], RECONCILED)
                contests = set(report["contests"])
                if source_id in SOME_HOUSE_RACES:
                    self.assertTrue(set(adapter.base_races) <= contests < set(adapter.races))
                else:
                    self.assertEqual(contests, set(adapter.races))
                # What the loader would copy is exactly what the parse wrote.
                directory = Path(report["output_dir"])
                store = ArchiveStore(root, source)
                converted = copies(source, SETTINGS, store, directory, 88)[("landing", "results")]
                self.assertEqual(len(converted), report["output_rows"])
                self.assertEqual(converted[0][:2], (88, 2))
                self.assertEqual(
                    parse_source(source, SETTINGS, root, sha256=sha)["outcome"], "already_parsed"
                )
                self.assertEqual(self.corrected(source_id, report), report["output_sha256"])

                geography = json.loads((STATIC / f"{adapter.slug}-map.json").read_text())
                if adapter.county_results:
                    keys = {c["id"] for c in geography["counties"]}
                    self.assertEqual(len(keys), len(geography["counties"]), "unique county keys")
                house = {r for r in adapter.races if not r.endswith(("-SEN", "-GOV"))}
                self.assertLessEqual(house, {d["id"] for d in geography["districts"]})
                payload = make_payload(
                    geography,
                    {"status": "unofficial"},
                    counted(report, "county_results.csv", adapter.methods),
                    counted(report, "contest_results.csv", adapter.methods),
                    state=source.state,
                    county_feeds=feeds(report) if adapter.county_feed_status else None,
                )
                # Every parsed race is shown; a state whose House is optional may show
                # only the races it has so far.
                self.assertTrue(contests <= set(payload["races"]) <= set(adapter.races))
                if not adapter.optional_house:
                    self.assertEqual(set(payload["races"]), set(adapter.races))
                self.assertEqual(payload["county_detail"], adapter.county_results)
                self.assertEqual(payload["methods"], adapter.methods)
                totals = {r: payload["races"][r]["stats"]["total_votes"]["total"] for r in contests}
                # A synthetic export fills only some races; a capture has votes in each.
                self.assertTrue(
                    any(totals.values()) if source_id in SYNTHETIC else all(totals.values()), totals
                )
                for race in contests:
                    stats = payload["races"][race]["stats"]
                    if len(adapter.methods) > 1 and race not in adapter.ranked_choice:
                        self.assertEqual(
                            sum(stats[m]["total"] for m in adapter.methods[1:]),
                            stats["total_votes"]["total"],
                            race,
                        )
                if adapter.county_results and source_id not in SOME_COUNTIES:
                    # A statewide race is counted in every county on the map.
                    for race in contests & {source.state + "-SEN", source.state + "-GOV"}:
                        drawn = payload["races"][race]["counties"]
                        self.assertEqual(len(drawn), len(geography["counties"]), race)
                for race, number in COUNTY_COUNTS.get(source_id, {}).items():
                    self.assertEqual(report["county_counts"][race], number, race)
                providers[source.state] = lambda payload=payload: payload
        self.assertEqual(set(national_payload(providers)["states"]), set(STATES))

    def corrected(self, source_id, report):
        """The snapshot's output when parsed beside an earlier one with higher counts."""
        first, root = self.root / source_id, self.root / (source_id + "-corrected")
        # The same stored snapshot (a collection is not repeated: a bundle records its time).
        shutil.copytree(first, root, ignore=lambda folder, names: {"parsed"} & set(names))
        earlier = root / "parsed" / source_id / ("0" * 16 + "-" + "0" * 16)
        shutil.copytree(report["output_dir"], earlier)
        inflated(earlier)
        sha = report["identity"]["raw_sha256"]
        synthetic = recording() if source_id in SYNTHETIC else contextlib.nullcontext()
        with synthetic:
            again = parse_source(SOURCES[source_id], SETTINGS, root, sha256=sha)
        self.assertEqual(again["outcome"], "parsed", again)
        return again["output_sha256"]


def publishes(source_id):
    """A database class publishing one source, so shards balance by source."""

    @requires_database
    class Publishes(unittest.TestCase):
        def test_loads_validates_publishes_and_the_dashboard_reads_only_serving(self):
            db = migrated()
            source = SOURCES[source_id]
            adapter = state_adapter(source.state)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, report = parsed(source_id, root)
                self.assertEqual(report["outcome"], "parsed", report)
                loaded = load_parsed(source, SETTINGS, root, Path(report["output_dir"]), db)
                self.assertEqual(loaded["outcome"], "succeeded", loaded)
                candidate = publication.prepare(ROOT, db, loaded["release_id"], "test")
                self.assertEqual(candidate["outcome"], "validated", candidate)
                published = publication.publish_prepared(ROOT, db, candidate["candidate_id"])
                self.assertEqual(published["outcome"], "published", published)
            with connect(db) as conn:
                conn.execute("SET ROLE election_dashboard")
                where = " WHERE publication_id=%s"
                key = (published["publication_id"],)
                contests = conn.execute(
                    "SELECT count(DISTINCT contest_id) AS n, sum(total_votes) AS total, "
                    "array_agg(DISTINCT state) AS states FROM serving.contest_results" + where,
                    key,
                ).fetchone()
                self.assertEqual(contests["n"], PARSED[source_id][1])
                self.assertEqual(contests["states"], [source.state])
                if adapter.methods[1:]:
                    methods = conn.execute(
                        "SELECT coalesce(sum(votes), 0) AS n FROM serving.contest_method_results"
                        + where,
                        key,
                    ).fetchone()["n"]
                    self.assertEqual(methods, contests["total"])
                if adapter.county_results:
                    county = conn.execute(
                        "SELECT count(*) AS n FROM serving.county_results" + where, key
                    ).fetchone()["n"]
                    self.assertGreater(county, 0)
                # The dashboard reads published results only, never parsed rows.
                self.assertFalse(
                    conn.execute(
                        "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                    ).fetchone()["ok"]
                )

    Publishes.__name__ = Publishes.__qualname__ = f"Publishes_{source_id}"
    return Publishes


for _source_id in SNAPSHOTS:
    globals()[f"Publishes_{_source_id}"] = publishes(_source_id)
del _source_id


if __name__ == "__main__":
    unittest.main()
