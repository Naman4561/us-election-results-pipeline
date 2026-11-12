"""Every Senate, Governor and House race of the states with large House delegations."""

import base64
import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from loading import migrated, requires_database

from election_pipeline import bundles, publication
from election_pipeline.collect import FetchResponse
from election_pipeline.dashboard import server
from election_pipeline.parsers import mn
from election_pipeline.parsers.nc import RESULT_HEADER
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, file_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/full_races"


def members(code):
    """Synthetic additions to the existing documented fixtures; no real candidate claim."""
    if code == "NC":
        rows = []
        for race in state_adapter(code).races:
            title = (
                "US SENATE"
                if race == "NC-SEN"
                else "US HOUSE OF REPRESENTATIVES DISTRICT " + race[-2:]
            )
            for choice, party in (("Example A", "DEM"), ("Example B", "REP")):
                rows.append(
                    [
                        "WAKE",
                        "11/03/2026",
                        "001",
                        "1",
                        "S",
                        title,
                        choice,
                        party,
                        "1",
                        "1",
                        "2",
                        "3",
                        "4",
                        "10",
                        "Y",
                    ]
                )
        rows.append(
            [
                "WAKE",
                "11/03/2026",
                "001",
                "1",
                "S",
                "US HOUSE OF REPRESENTATIVES DISTRICT 02",
                "Write-In (Miscellaneous)",
                "\x00",
                "1",
                "1",
                "2",
                "3",
                "4",
                "10",
                "Y",
            ]
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t")
        writer.writerow(RESULT_HEADER)
        writer.writerows(rows)
        return {"results_pct_20261103.txt": output.getvalue().encode()}
    if code == "MN":
        data = {
            name: (FIXTURES / ("mn-" + name)).read_bytes()
            for name in (
                "senate-county.txt",
                "senate.txt",
                "house-county.txt",
                "house.txt",
                "counties.txt",
            )
        }
        counties = list(csv.reader(io.StringIO(data["counties.txt"].decode()), delimiter=";"))
        total = sum(int(row[2]) for row in counties)
        state, precinct = [], []
        for candidate, name, party in (("0001", "Example A", "DFL"), ("0002", "Example B", "R")):
            state.append(
                [
                    "MN",
                    "",
                    "",
                    "0331",
                    "Governor & Lt Governor",
                    "",
                    candidate,
                    name,
                    "",
                    "",
                    party,
                    str(total),
                    str(total),
                    str(total),
                    "50",
                    str(2 * total),
                ]
            )
            for county, _, number in counties:
                for unit in range(int(number)):
                    precinct.append(
                        [
                            "MN",
                            county,
                            f"{unit:04}",
                            "0331",
                            "Governor & Lt Governor",
                            "",
                            candidate,
                            name,
                            "",
                            "",
                            party,
                            "1",
                            "1",
                            "1",
                            "50",
                            "2",
                        ]
                    )
        for name, rows in (("governor.txt", state), ("governor-precinct.txt", precinct)):
            data[name] = ("\n".join(";".join(row) for row in rows) + "\n").encode()
        return data
    if code == "NE":
        data = (FIXTURES / "ne.csv").read_bytes()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        for district in range(1, 4):
            for candidate, party in ((1, "DEM"), (2, "REP")):
                writer.writerow(
                    [
                        100 + district,
                        "For Representative in Congress - 2  Year Term",
                        party,
                        "CG",
                        f"District {district:02}",
                        "103",
                        "",
                        candidate,
                        f"Example {candidate}",
                        "11/4/2026 1:00:00 PM",
                        "1",
                        "5",
                        "50",
                        '="1/2"',
                        "",
                    ]
                )
        return {
            "results.csv": data + output.getvalue().encode(),
            "election.html": b"Unofficial Results General Election November 3, 2026",
        }
    from test_texas import encoded, fixture

    return {"results.json": encoded(fixture())}


def parse_fixture(code, data, root):
    settings, sources = load_config(ROOT / "config/sources.toml")
    source = sources[code.lower() + "_2026_general_all"]
    archive = root / "input.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, body in data.items():
            zipped.writestr(name, body)
    digest = file_hash(archive)
    target = ArchiveStore(root, source).archive_path(digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive.replace(target)
    return source, settings, parse_source(source, settings, root, sha256=digest)


class FullRaceParserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def parse(self, code, data=None):
        return parse_fixture(code, members(code) if data is None else data, self.root)

    def test_all_70_races_parse_and_match_existing_map_districts(self):
        for code, count in (("TX", 40), ("MN", 10), ("NC", 15), ("NE", 5)):
            with self.subTest(state=code):
                source, _, result = self.parse(code)
                self.assertEqual(result["outcome"], "parsed", result)
                self.assertEqual(len(result["contests"]), count)
                geo = json.loads(
                    (server.STATIC / (code.lower() + "-map.json")).read_text(encoding="utf-8")
                )
                expected = state_adapter(code).races
                self.assertEqual(
                    set(expected) - {code + "-SEN", code + "-GOV"},
                    {r["id"] for r in geo["districts"]},
                )

                def read(name):
                    with (Path(result["output_dir"]) / name).open(
                        encoding="utf-8", newline=""
                    ) as handle:
                        return list(csv.DictReader(handle))

                contests, counties = read("contest_results.csv"), read("county_results.csv")
                for row in (*contests, *counties):
                    for field in state_adapter(code).methods:
                        row[field] = int(row[field])
                payload = server.make_payload(
                    geo, {"publication_id": "p"}, counties, contests, state=code, races=expected
                )
                self.assertEqual(set(payload["races"]), set(expected))
                if code == "MN":
                    self.assertEqual(result["county_counts"]["MN-GOV"], 87)
                    self.assertEqual(result["county_reconciliation"], "passed")
                if code == "TX":
                    self.assertEqual(
                        len(payload["races"]["TX-13"]["stats"]["total_votes"]["candidates"]), 1
                    )

    def test_new_house_race_missing_from_each_full_export_blocks_publication(self):
        for code in ("NC", "MN", "NE", "TX"):
            data = members(code)
            if code == "NC":
                key = "results_pct_20261103.txt"
                data[key] = b"\n".join(
                    line for line in data[key].splitlines() if b"DISTRICT 14" not in line
                )
            elif code == "MN":
                for key in ("house.txt", "house-county.txt"):
                    data[key] = b"\n".join(
                        line for line in data[key].splitlines() if b";0111;" not in line
                    )
            elif code == "NE":
                data["results.csv"] = b"\n".join(
                    line for line in data["results.csv"].splitlines() if b"District 03" not in line
                )
            else:
                envelope = json.loads(data["results.json"])
                federal = json.loads(base64.b64decode(envelope["Federal"]))
                federal["Races"] = [
                    r for r in federal["Races"] if not r["N"].endswith("DISTRICT 1")
                ]
                envelope["Federal"] = base64.b64encode(json.dumps(federal).encode()).decode()
                data["results.json"] = json.dumps(envelope).encode()
            with self.subTest(state=code):
                self.assertEqual(self.parse(code, data)[2]["outcome"], "validation_failed")

    def test_governor_missing_zero_vote_precinct_is_rejected(self):
        data = members("MN")
        for name in ("governor.txt", "governor-precinct.txt"):
            rows = list(csv.reader(io.StringIO(data[name].decode()), delimiter=";"))
            for row in rows:
                row[13] = row[15] = "0"
            if "precinct" in name:
                rows = rows[1:]
            data[name] = ("\n".join(";".join(row) for row in rows) + "\n").encode()
        self.assertEqual(self.parse("MN", data)[2]["outcome"], "validation_failed")

    def test_precinct_governor_votes_and_nebraska_district_identity_are_checked(self):
        data = members("MN")
        data["governor-precinct.txt"] += data["governor-precinct.txt"].splitlines(keepends=True)[0]
        self.assertEqual(self.parse("MN", data)[2]["outcome"], "validation_failed")
        data = members("NE")
        data["results.csv"] = data["results.csv"].replace(b"District 01", b"District 99")
        self.assertEqual(self.parse("NE", data)[2]["outcome"], "validation_failed")

    def test_dbt_receives_the_state_contract(self):
        _, _, parsed = self.parse("TX")
        metadata = dict(
            state="TX",
            dataset="election_results",
            source_id="tx_2026_general_all",
            release_id=7,
            election_date="2026-11-03",
            parser_report=parsed,
        )
        with (
            patch.object(publication.subprocess, "run") as run,
            patch.object(publication, "checked_results"),
        ):
            run.return_value.returncode = 0
            for cache in ("partial_parse.msgpack", "graph.gpickle", "manifest.json"):
                (self.root / cache).write_bytes(b"dbt")
            publication.build_candidate(
                ROOT,
                {
                    "host": "localhost",
                    "port": 5432,
                    "dbname": "test",
                    "user": "writer",
                    "password": "secret",
                },
                metadata,
                "p_test",
                self.root,
            )
        command = run.call_args.args[0]
        variables = json.loads(command[command.index("--vars") + 1])
        self.assertEqual(len(variables["races"]), 40)
        self.assertEqual(variables["state"], "TX")
        self.assertEqual(variables["county_reconciliation"], "none")
        self.assertEqual(command[command.index("--select") + 1], "tag:results")
        # Parse caches go; the audited manifest stays.
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.endswith((".msgpack", ".gpickle"))], []
        )
        self.assertTrue((self.root / "manifest.json").exists())

    def test_mn_county_and_official_totals_must_agree(self):
        def positive(data):
            for name in ("senate-county.txt", "senate.txt"):
                rows = list(csv.reader(io.StringIO(data[name].decode()), delimiter=";"))
                for row in rows:
                    # Give every candidate one vote in every county.
                    row[13], row[15] = ("1", "5") if "county" in name else ("87", "435")
                data[name] = ("\n".join(";".join(row) for row in rows)).encode()
            return data

        _, _, good = self.parse("MN", positive(members("MN")))
        self.assertEqual(good["outcome"], "parsed", good)
        self.assertFalse(good["all_counts_zero"])
        mismatched = positive(members("MN"))
        mismatched["senate.txt"] = mismatched["senate.txt"].replace(b";87;", b";86;", 1)
        self.assertEqual(self.parse("MN", mismatched)[2]["outcome"], "validation_failed")

    def test_mn_missing_county_and_html_data_are_rejected(self):
        data = members("MN")
        data["senate-county.txt"] = b"\n".join(data["senate-county.txt"].splitlines()[5:])
        self.assertEqual(self.parse("MN", data)[2]["outcome"], "validation_failed")
        data = members("MN")
        data["senate.txt"] = b"<html><title>Radware Captcha Page</title></html>"
        result = self.parse("MN", data)[2]
        self.assertEqual(result["outcome"], "validation_failed")
        self.assertIn("received HTML", result["error"])

    def test_mn_election_paths_and_official_host_are_required(self):
        _, sources = load_config(ROOT / "config/sources.toml")
        source = sources["mn_2026_general_all"]
        for bad in (
            replace(source, election_date="2024-11-05"),
            replace(source, url=source.url.replace("20261103", "20241105")),
            replace(
                source, url=source.url.replace("electionresultsfiles.sos.mn.gov", "example.com")
            ),
            replace(
                source,
                files=tuple(
                    (name, url.replace("20261103", "20241105")) for name, url in source.files
                ),
            ),
        ):
            with self.subTest(url=bad.url), self.assertRaisesRegex(ValueError, "election-dated"):
                mn.validate_source(bad)

    def test_mn_collector_fetches_exactly_its_seven_data_files(self):
        settings, sources = load_config(ROOT / "config/sources.toml")
        source = sources["mn_2026_general_all"]
        data = members("MN")
        by_url = {
            url: data[name] for name, url in (*source.files, (source.primary_filename, source.url))
        }

        def fetch(source, settings, path, headers, *, url):
            body = by_url[url]
            path.write_bytes(body)
            return FetchResponse(200, {}, url, len(body), hashlib.sha256(body).hexdigest())

        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch) as request:
            path = self.root / "media.zip"
            bundles.fetch_bundle(source, settings, path)
            self.assertEqual(request.call_count, 7)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), set(source.expected_members))

    def test_mn_and_ne_reject_duplicates_missing_races_and_wrong_elections(self):
        for code, key, row in (("MN", "senate.txt", 0), ("NE", "results.csv", 1)):
            with self.subTest(code):
                data = members(code)
                data[key] += data[key].splitlines(keepends=True)[row]
                self.assertEqual(self.parse(code, data)[2]["outcome"], "validation_failed")
        data = members("NE")
        data["election.html"] = data["election.html"].replace(b"2026", b"2024")
        self.assertEqual(self.parse("NE", data)[2]["outcome"], "validation_failed")
        data = members("MN")
        data["senate.txt"] = b""
        self.assertEqual(self.parse("MN", data)[2]["outcome"], "validation_failed")
        data = members("NE")
        data["results.csv"] = data["results.csv"].replace(
            b"For Governor and Lt. Governor", b"Other"
        )
        self.assertEqual(self.parse("NE", data)[2]["outcome"], "validation_failed")

    def test_ne_only_allows_one_empty_trailing_column_and_literal_precinct_expression(self):
        for old, new in [
            (b',="0/1322",', b",=2+2,"),
            (b",1,0,0,", b",1,-1,0,"),
            (b'"SW"', b'"CTY"'),
            (b'1322",', b'1322",EXTRA,'),
        ]:
            with self.subTest(old=old):
                data = members("NE")
                data["results.csv"] = data["results.csv"].replace(old, new, 1)
                self.assertEqual(self.parse("NE", data)[2]["outcome"], "validation_failed")

    def test_ne_records_official_only_from_its_page_label(self):
        self.assertIs(self.parse("NE")[2]["official_results"], False)

        def labeled(label):
            data = members("NE")
            data["election.html"] = data["election.html"].replace(b"Unofficial Results", label)
            return self.parse("NE", data)[2]

        self.assertIs(labeled(b"Official Results")["official_results"], True)
        self.assertEqual(labeled(b"Results")["outcome"], "validation_failed")


@requires_database
class FullRaceDatabaseTests(unittest.TestCase):
    def test_loads_publish_into_shared_tables_the_dashboard_may_read(self):
        from election_pipeline.database import connect, load_parsed

        db = migrated()
        for code, count in (("TX", 40), ("MN", 10), ("NC", 15), ("NE", 5)):
            with self.subTest(code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, settings, parsed = parse_fixture(code, members(code), root)
                self.assertEqual(parsed["outcome"], "parsed", parsed)
                loaded = load_parsed(source, settings, root, Path(parsed["output_dir"]), db)
                self.assertEqual(loaded["outcome"], "succeeded", loaded)
                checked = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
                published = publication.publish_prepared(ROOT, db, checked["candidate_id"])
                self.assertEqual(published["outcome"], "published", published)
                with connect(db) as conn:
                    conn.execute("SET ROLE election_dashboard")
                    rows = conn.execute(
                        "SELECT * FROM serving.contest_results WHERE publication_id=%s",
                        (published["publication_id"],),
                    ).fetchall()
                    self.assertEqual(len({row["contest_id"] for row in rows}), count)
                    self.assertEqual({row["state"] for row in rows}, {code})
                    if code == "NC":
                        write_ins = [
                            row for row in rows if row["choice"] == "Write-In (Miscellaneous)"
                        ]
                        self.assertEqual(len(write_ins), 1)
                        self.assertIsNone(write_ins[0]["choice_party"])
                        self.assertEqual(write_ins[0]["total_votes"], 10)
                        methods = conn.execute(
                            "SELECT method, votes FROM serving.contest_method_results "
                            "WHERE publication_id=%s AND choice=%s",
                            (published["publication_id"], "Write-In (Miscellaneous)"),
                        ).fetchall()
                        self.assertEqual(
                            {m["method"]: m["votes"] for m in methods},
                            {
                                "election_day": 1,
                                "early_voting": 2,
                                "absentee_by_mail": 3,
                                "provisional": 4,
                            },
                        )
                    # The dashboard reads published results only, never parsed rows.
                    self.assertFalse(
                        conn.execute(
                            "SELECT has_schema_privilege(current_user, 'landing', 'USAGE') AS ok"
                        ).fetchone()["ok"]
                    )


if __name__ == "__main__":
    unittest.main()
