"""Full-race contracts, isolated source IDs, atomic public switching and recovery gates."""

import base64
import copy
import csv
import hashlib
import io
import json
import os
import socket
import tempfile
import unittest
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from election_pipeline import publication
from election_pipeline.dashboard import server
from election_pipeline.deployment import catalog, containers, control, coverage
from election_pipeline.parsers.nc import RESULT_HEADER
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore, file_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/expansion"


def members(code):
    """Synthetic additions to the existing documented fixtures; no real candidate claim."""
    if code == "NC":
        rows = []
        for race in state_adapter(code).available_races:
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
    from test_texas_civix import fixture

    data = fixture()
    existing = {race["N"] for race in data["Federal"]["Races"]}
    for district in range(1, 39):
        title = f"U. S. REPRESENTATIVE DISTRICT {district}"
        if title not in existing:
            candidates = [{"ID": district * 10, "N": "Synthetic A", "P": "REP", "V": 5}]
            if district != 13:
                candidates.append({"ID": district * 10 + 1, "N": "Synthetic B", "P": "DEM", "V": 3})
            data["Federal"]["Races"].append(
                {
                    "id": 1000 + district,
                    "N": title,
                    "Candidates": candidates,
                    "T": sum(c["V"] for c in candidates),
                }
            )
    encoded = {
        key: value if key == "Version" else base64.b64encode(json.dumps(value).encode()).decode()
        for key, value in data.items()
    }
    return {"results.json": json.dumps(encoded).encode()}


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


class CoverageParserTests(unittest.TestCase):
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
                expected = state_adapter(code).selected_races(source)
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

    def test_legacy_source_signature_and_unsupported_contest_guards(self):
        settings, sources = load_config(ROOT / "config/sources.toml")
        source = sources["nc_2026_general"]
        from election_pipeline import __version__

        raw = asdict(source)
        raw.pop("contests")
        raw.pop("primary_filename")
        raw.pop("supplemental_members")
        raw.pop("collector_overrides")
        old = {
            "source": raw,
            "validator_version": __version__,
            **{
                field: getattr(settings, field)
                for field in (
                    "max_download_bytes",
                    "max_uncompressed_bytes",
                    "max_zip_members",
                    "max_csv_rows",
                )
            },
        }
        self.assertEqual(
            source.signature(settings),
            hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest(),
        )
        with self.assertRaises(ValueError):
            state_adapter("NC").selected_races(replace(source, contests=("NC-GOV",)))
        with self.assertRaises(ValueError):
            replace(source, contests=("NC-01", "NC-01"))
        with self.assertRaises(ValueError):
            replace(source, contests=("TX-01",))

    def test_dbt_receives_the_release_contest_contract_and_uncontested_policy(self):
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
        self.assertEqual(len(variables["tx_races"]), 40)
        self.assertTrue(variables["allow_uncontested"])
        self.assertEqual(variables["tx_results_source"], metadata["source_id"])


class CoverageDeploymentTests(unittest.TestCase):
    def setUp(self):
        from test_deployment import fixture

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        self.value = fixture(self.root)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")
        old = self.value["sources"]["nc_2026_general"]
        old["source"] = json.loads(json.dumps(asdict(self.sources["nc_2026_general"])))
        old["races"] = list(state_adapter("NC").races)
        old["status"] = "unofficial"
        self.old = copy.deepcopy(old)
        self.name = "nc_2026_general_all"
        entry = catalog.entry(
            self.sources[self.name],
            self.settings,
            image_id="sha256:" + "d" * 64,
            revision="e" * 40,
            release_path=self.root,
            worker="expansion",
            status="unofficial",
        )
        entry.update(replaces="nc_2026_general", races=list(self.sources[self.name].contests))
        self.entry = entry
        self.prod_file = self.root / "production.env"
        self.original = (
            "IMAGE_TAG="
            + self.value["baseline"]["revision"]
            + "\nPIPELINE_SOURCES=nc_2026_general,ky_2026_general\nPOSTGRES_PASSWORD=keep-private\n"
            + "DATA_DIR="
            + str(self.root / "data")
            + "\n"
        )
        self.prod_file.write_text(self.original)
        self.prod = {
            **dict(line.split("=", 1) for line in self.original.splitlines()),
            "DATA_DIR": str(self.root / "data"),
        }
        self.config = {"CONTROL_STATE_DIR": str(self.root), "PRODUCTION_ENV": str(self.prod_file)}
        catalog.write(self.path, self.value)

    def add(self):
        self.value["sources"][self.name] = copy.deepcopy(self.entry)
        catalog.write(self.path, self.value)
        self.value = catalog.read(self.path)

    def test_only_explicit_replacements_may_share_a_state_and_only_one_is_public(self):
        self.add()
        self.assertEqual(catalog.public_states(self.value), {"NC", "KY"})
        proposed = catalog.prospective(self.value, [self.name])
        self.assertFalse(proposed["sources"]["nc_2026_general"]["public"])
        self.assertTrue(proposed["sources"]["nc_2026_general"]["enabled"])
        self.assertTrue(proposed["sources"][self.name]["public"])
        self.assertEqual(self.value["sources"]["nc_2026_general"], self.old)
        proposed["sources"]["nc_2026_general"]["public"] = True
        with self.assertRaises(ValueError):
            catalog.validate(proposed)
        self.value["sources"][self.name].pop("replaces")
        with self.assertRaises(ValueError):
            catalog.validate(self.value)

    def repin(self, mutate_meta=None, migrations=None):
        meta = {
            "source": json.loads(json.dumps(asdict(self.sources[self.name]))),
            "settings": asdict(self.settings),
            "races": list(self.sources[self.name].contests),
            "code_sha256": "c" * 64,
        }
        if mutate_meta:
            mutate_meta(meta)
        with (
            patch.object(containers, "metadata", return_value=meta),
            patch.object(containers, "package_hash", return_value="c" * 64),
            patch.object(containers, "release_hash", return_value="b" * 64),
            patch.object(control, "db_settings", return_value={}),
            patch.object(control, "connect") as connect,
        ):
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value.fetchall.return_value = migrations or []
            coverage.repin_private(
                self.path, self.root, "sha256:" + "f" * 64, "a" * 40, self.name, self.prod
            )

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_corrected_image_can_repin_only_private_sources_and_requires_fresh_validation(self):
        self.add()
        self.value["sources"][self.name].update(
            candidate_id="old-candidate",
            publication_id="old-private",
            published_at="2026-11-04T20:00:00+00:00",
        )
        catalog.write(self.path, self.value)
        self.repin()
        after = catalog.read(self.path)
        current = after["sources"][self.name]
        self.assertEqual(current["image_id"], "sha256:" + "f" * 64)
        self.assertEqual(current["revision"], "a" * 40)
        self.assertFalse(current["public"])
        self.assertFalse(current["enabled"])
        self.assertNotIn("candidate_id", current)
        self.assertNotIn("publication_id", current)
        self.assertNotIn("published_at", current)
        self.assertEqual(current["previous_preparation"]["publication_id"], "old-private")
        for name in self.value["sources"]:
            if name != self.name:
                self.assertEqual(after["sources"][name], self.value["sources"][name])
        self.assertEqual(self.prod_file.read_text(), self.original)

    def test_repin_rejects_an_enabled_or_public_source_before_inspecting_an_image(self):
        self.add()
        for public in (False, True):
            value = copy.deepcopy(self.value)
            if public:
                value = catalog.prospective(value, [self.name])
            else:
                value["sources"][self.name]["enabled"] = True
            catalog.write(self.path, value)
            before = self.path.read_bytes()
            with patch.object(containers, "metadata") as metadata:
                with self.assertRaisesRegex(ValueError, "disabled, private"):
                    coverage.repin_private(
                        self.path, self.root, "sha256:" + "f" * 64, "a" * 40, self.name, self.prod
                    )
            metadata.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_repin_rejects_changed_source_settings_and_race_coverage(self):
        self.add()
        before = self.path.read_bytes()
        for mutate in (
            lambda meta: meta["source"].update(url="https://example.com/changed.zip"),
            lambda meta: meta["settings"].update(max_csv_rows=1),
            lambda meta: meta["races"].pop(),
        ):
            with self.assertRaisesRegex(ValueError, "cannot change"):
                self.repin(mutate)
            self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_private_repin_accepts_and_records_a_new_user_agent(self):
        self.add()
        agent = "US-Election-Results-Pipeline/0.1 (+https://example.com/project)"
        self.repin(lambda meta: meta["settings"].update(user_agent=agent))
        entry = catalog.read(self.path)["sources"][self.name]
        self.assertEqual(entry["settings"]["user_agent"], agent)

    def test_repin_rejects_changed_applied_migrations(self):
        self.add()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "applied migration"):
            self.repin(migrations=[{"name": "001_initial.sql", "sha256": "0" * 64}])
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_register_stages_separate_source_without_modifying_the_live_owner(self):
        source = self.sources[self.name]
        meta = {
            "source": json.loads(json.dumps(asdict(source))),
            "settings": asdict(self.settings),
            "races": list(source.contests),
            "code_sha256": "c" * 64,
        }
        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "db_settings"),
            patch.object(control, "connect"),
            patch.object(control.os, "fchown"),
            patch.object(
                containers,
                "verify_release",
                return_value=(self.entry["image_id"], self.entry["revision"]),
            ),
            patch.object(containers, "metadata", return_value=meta),
            patch.object(containers, "package_hash", return_value="c" * 64),
            patch.object(containers, "release_hash", return_value="d" * 64),
            patch.object(containers, "command", return_value="inactive"),
        ):
            control.register(
                self.path,
                self.root,
                "image",
                self.name,
                "unofficial",
                self.prod,
                self.config,
                replaces="nc_2026_general",
            )
        after = catalog.read(self.path)
        self.assertEqual(after["sources"]["nc_2026_general"], self.old)
        self.assertFalse(after["sources"][self.name]["public"])
        self.assertFalse(after["sources"][self.name]["enabled"])
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.original)

    def test_dashboard_reads_the_catalog_source_id_before_and_after_the_switch(self):
        self.add()

        def payload(provider):
            return {
                "state": provider.state.code,
                "races": {r: {} for r in provider.races},
                "publication": {"source_id": provider.source_id},
            }

        with (
            patch.object(server, "dashboard_settings", return_value={}),
            patch.object(server.PublishedResults, "__call__", payload),
        ):
            client = TestClient(server.create_app(catalog_file=self.path))
            before = client.get("/api/national").json()
            self.assertEqual(before["states"]["NC"]["publication"]["source_id"], "nc_2026_general")
            self.assertEqual(len(before["coverage"]["NC"]), 5)
            self.value = catalog.prospective(self.value, [self.name])
            catalog.write(self.path, self.value)
            after = client.get("/api/national").json()
            self.assertEqual(after["states"]["NC"]["publication"]["source_id"], self.name)
            self.assertEqual(len(after["coverage"]["NC"]), 15)

    def test_automatic_port_selection_skips_docker_and_native_listeners(self):
        with socket.socket() as occupied:
            try:
                occupied.bind(("127.0.0.1", 8001))
            except OSError:
                pass  # An existing native listener also makes this port unavailable.
            with patch.object(containers, "command", return_value="127.0.0.1:8002->8000/tcp"):
                self.assertGreaterEqual(coverage.free_port(), 8003)

    def test_preparation_waits_for_old_control_jobs_before_changing_catalog(self):
        sleeps = []

        def response(arguments, **kwargs):
            if arguments[-2] == "ExecStart":
                return str(self.root / "scripts/deploy.py")
            return (
                "activating"
                if not sleeps and arguments[-1] == "election-expansion.service"
                else "inactive"
            )

        before = self.path.read_bytes()
        with (
            patch.object(control, "controls_ready"),
            patch.object(containers, "command", side_effect=response),
            patch.object(coverage.time, "sleep", side_effect=lambda delay: sleeps.append(delay)),
        ):
            coverage.wait_for_controls({**self.config, "CONTROL_ROOT": str(self.root)})
        self.assertEqual(sleeps, [2])
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_restore_blocks_dashboard_creation_and_public_activation(self):
        self.add()
        self.value["sources"][self.name].update(
            publication_id="new-p", published_at=datetime.now(timezone.utc).isoformat()
        )
        catalog.write(self.path, self.value)
        backups = self.root / "backups"
        backups.mkdir()
        (backups / "latest-backup.json").write_text(
            json.dumps({"outcome": "uploaded", "backup_id": "backup_" + "a" * 32})
        )
        config = {**self.config, "CONTROL_ROOT": str(self.root), "BACKUP_DIR": str(backups)}
        with (
            patch.object(
                containers, "command", side_effect=["", json.dumps({"outcome": "failed"})]
            ),
            patch.object(coverage.dashboard, "start") as start,
            patch.object(control, "activate") as activate,
        ):
            with self.assertRaisesRegex(ValueError, "restore"):
                coverage.activate(self.path, ["NC"], self.prod, config)
        start.assert_not_called()
        activate.assert_not_called()
        self.assertEqual(catalog.read(self.path), self.value)

    def test_activation_uses_fresh_restored_publication_and_an_automatically_selected_port(self):
        self.add()
        now = datetime.now(timezone.utc)
        self.value["sources"][self.name].update(
            publication_id="new-p", published_at=(now - timedelta(minutes=1)).isoformat()
        )
        catalog.write(self.path, self.value)
        backups = self.root / "backups"
        backups.mkdir()
        (backups / "latest-backup.json").write_text(
            json.dumps({"outcome": "uploaded", "backup_id": "backup_" + "a" * 32})
        )
        restored = {
            "outcome": "passed",
            "temporary_database_removed": True,
            "writer_permissions_verified": True,
            "reader_permissions_verified": True,
            "finished_at": now.isoformat(),
            "report_path": "restored.json",
            "current_publications": {self.name: "new-p"},
            "database_inventory": {"nc.contest_candidate_results": {"rows": 30}},
        }
        config = {**self.config, "CONTROL_ROOT": str(self.root), "BACKUP_DIR": str(backups)}
        with (
            patch.object(containers, "command", side_effect=["", json.dumps(restored)]),
            patch.object(coverage, "free_port", return_value=8009),
            patch.object(
                coverage.dashboard, "start", return_value={"url": "http://127.0.0.1:8009"}
            ) as start,
            patch.object(coverage.dashboard, "check"),
            patch.object(control, "activate", return_value={"outcome": "activated"}) as activate,
        ):
            result = coverage.activate(self.path, ["NC"], self.prod, config)
        self.assertEqual(result["outcome"], "activated")
        self.assertEqual(start.call_args.kwargs["port"], 8009)
        self.assertEqual(activate.call_args.args[2], restored)
        self.assertEqual(catalog.read(self.path), self.value)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_retirement_changes_only_future_source_selection_and_preserves_images(self):
        self.add()
        self.value = catalog.prospective(self.value, [self.name])
        catalog.write(self.path, self.value)
        with (
            patch.object(control, "controls_ready"),
            patch.object(containers, "command", return_value="inactive"),
        ):
            result = coverage.retire(self.path, ["NC"], self.prod, self.config)
        self.assertEqual(result["outcome"], "retired")
        after = catalog.read(self.path)
        self.assertEqual(after["sources"]["nc_2026_general"]["image_id"], self.old["image_id"])
        self.assertEqual(after["sources"][self.name], self.value["sources"][self.name])
        self.assertEqual(
            after["sources"]["ky_2026_general"], self.value["sources"]["ky_2026_general"]
        )
        self.assertEqual(after["sources"]["nc_2026_general"]["worker"], "expansion")
        self.assertFalse(after["sources"]["nc_2026_general"]["enabled"])
        self.assertIn(
            "PIPELINE_SOURCES=ky_2026_general\n", self.prod_file.read_text(encoding="utf-8")
        )
        self.assertIn("POSTGRES_PASSWORD=keep-private", self.prod_file.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_retirement_rejects_active_cycle_and_rolls_back_a_racing_cycle(self):
        self.add()
        self.value = catalog.prospective(self.value, [self.name])
        catalog.write(self.path, self.value)
        for states in (("activating",), ("inactive", "activating")):
            with (
                self.subTest(states=states),
                patch.object(control, "controls_ready"),
                patch.object(containers, "command", side_effect=states),
            ):
                with self.assertRaisesRegex(ValueError, "cycle is running"):
                    coverage.retire(self.path, ["NC"], self.prod, self.config)
            self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.original)
            self.assertEqual(catalog.read(self.path), self.value)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_disable_baseline_keeps_other_states_and_can_resume_after_a_racing_cycle(self):
        name = "ky_2026_general"
        self.value["previous_dashboard"] = copy.deepcopy(self.value["dashboard"])
        catalog.write(self.path, self.value)
        before = catalog.read(self.path)
        caddyfile = self.root / "Caddyfile"
        original_route = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        caddyfile.write_text(original_route, encoding="utf-8")
        config = {**self.config, "CADDYFILE": str(caddyfile), "CADDY_CONTAINER": "caddy"}

        def start(value, sources, prod, config, directory, **kwargs):
            self.assertEqual(sources, [])
            self.assertEqual(catalog.public_states(value), {"NC"})
            self.assertEqual(kwargs["image"], self.value["baseline"]["image_id"])
            return {
                "name": "election-expansion-dashboard-" + directory.name.removeprefix("dashboard-"),
                "url": "http://localhost:8001",
                "states": ["NC"],
            }

        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "require_no_backup"),
            patch.object(containers, "command", side_effect=["inactive", "inactive", "active"]),
            patch.object(coverage, "free_port", return_value=8001),
            patch.object(control.dashboard, "start", side_effect=start),
            patch.object(control.dashboard, "check") as check,
            patch.object(control.dashboard, "reload"),
        ):
            with self.assertRaisesRegex(ValueError, "collector retirement did not finish"):
                coverage.disable_baseline(self.path, name, self.prod, config)
        hidden = catalog.read(self.path)
        self.assertEqual(catalog.public_states(hidden), {"NC"})
        self.assertTrue(hidden["sources"][name]["enabled"])
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.original)
        self.assertNotIn("previous_dashboard", hidden)
        self.assertIn(hidden["dashboard"]["name"] + ":8000", caddyfile.read_text())
        self.assertEqual([set(call.args[1]) for call in check.call_args_list], [{"NC"}, {"NC"}])

        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "require_no_backup"),
            patch.object(containers, "command", return_value="inactive"),
            patch.object(control.dashboard, "start") as start_dashboard,
        ):
            result = coverage.disable_baseline(self.path, name, self.prod, config)
            start_dashboard.assert_not_called()
            self.assertEqual(result["outcome"], "disabled")
            self.assertEqual(result["remaining_baseline_sources"], ["nc_2026_general"])
            prod = {**self.prod, "PIPELINE_SOURCES": "nc_2026_general"}
            repeated = coverage.disable_baseline(self.path, name, prod, config)
            self.assertEqual(repeated["outcome"], "already_disabled")
        after = catalog.read(self.path)
        self.assertEqual(after["dashboard"], hidden["dashboard"])
        self.assertEqual(
            after["sources"][name],
            {
                **before["sources"][name],
                "worker": "expansion",
                "enabled": False,
                "public": False,
            },
        )
        self.assertEqual(after["sources"]["nc_2026_general"], before["sources"]["nc_2026_general"])
        self.assertEqual(
            self.prod_file.read_text(encoding="utf-8"),
            self.original.replace("nc_2026_general,ky_2026_general", "nc_2026_general"),
        )

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_failed_disable_handoff_keeps_public_dashboard_and_schedule(self):
        before = catalog.read(self.path)
        caddyfile = self.root / "Caddyfile"
        route = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
        caddyfile.write_text(route, encoding="utf-8")
        config = {**self.config, "CADDYFILE": str(caddyfile), "CADDY_CONTAINER": "caddy"}
        with (
            patch.object(control, "controls_ready"),
            patch.object(control, "require_no_backup"),
            patch.object(containers, "command", return_value="inactive"),
            patch.object(coverage, "free_port", return_value=8001),
            patch.object(control.dashboard, "start", return_value={"url": "http://localhost:8001"}),
            patch.object(control.dashboard, "check", side_effect=[{}, ValueError("bad origin")]),
            patch.object(control.dashboard, "reload"),
            patch.object(control.dashboard, "stop_unused") as stop,
        ):
            with self.assertRaisesRegex(ValueError, "bad origin"):
                coverage.disable_baseline(self.path, "ky_2026_general", self.prod, config)
        stop.assert_called_once()
        self.assertEqual(catalog.read(self.path), before)
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.original)
        self.assertEqual(caddyfile.read_text(encoding="utf-8"), route)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_dashboard_rollback_restores_old_source_flags_after_retirement(self):
        self.add()
        self.value["previous_dashboard"] = {
            "name": "old",
            "url": "http://old",
            "states": ["NC", "KY"],
            "source_flags": {
                n: {k: e[k] for k in ("public", "enabled")}
                for n, e in self.value["sources"].items()
            },
        }
        self.value = catalog.prospective(self.value, [self.name])
        catalog.write(self.path, self.value)
        with (
            patch.object(control, "controls_ready"),
            patch.object(containers, "command", return_value="inactive"),
        ):
            coverage.retire(self.path, ["NC"], self.prod, self.config)
        with (
            patch.object(control.dashboard, "check"),
            patch.object(control, "switch_dashboard") as switch,
        ):
            control.rollback(self.path, self.config)
        after = switch.call_args.args[2]
        self.assertTrue(after["sources"]["nc_2026_general"]["public"])
        self.assertTrue(after["sources"]["nc_2026_general"]["enabled"])
        self.assertFalse(after["sources"][self.name]["public"])
        self.assertFalse(after["sources"][self.name]["enabled"])
        catalog.validate(after)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_interrupted_retirement_recovery_preserves_the_expanded_public_source(self):
        self.add()
        self.value = catalog.prospective(self.value, [self.name])
        catalog.write(self.path, self.value)
        with (
            patch.object(control, "controls_ready"),
            patch.object(containers, "command", return_value="inactive"),
        ):
            coverage.retire(self.path, ["NC"], self.prod, self.config)
        journal = self.root / "source-handoff.json"
        record = json.loads(journal.read_text(encoding="utf-8"))
        record["outcome"] = "pending"
        journal.write_text(json.dumps(record))
        with patch.object(containers, "command", return_value="inactive"):
            result = coverage.recover_retirement(self.path, self.config)
        self.assertEqual(result["outcome"], "recovered")
        self.assertEqual(catalog.read(self.path), self.value)
        self.assertTrue(catalog.read(self.path)["sources"][self.name]["public"])
        self.assertEqual(self.prod_file.read_text(encoding="utf-8"), self.original)


@unittest.skipUnless(os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable PostgreSQL *_test")
class CoverageDatabaseTests(unittest.TestCase):
    def test_all_four_loads_dbt_publications_and_reader_scopes(self):
        from election_pipeline.database import connect, connection_settings, load_parsed

        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        db = {**connection_settings(), "dbname": name}
        with connect(db) as conn:
            for relation, migration in (
                ("ops.sources", "001_initial.sql"),
                ("serving.publications", "002_publication.sql"),
                ("ky.results", "003_state_schemas.sql"),
                ("tx.results", "004_mn_ne_tx.sql"),
            ):
                if conn.execute("SELECT to_regclass(%s) AS t", (relation,)).fetchone()["t"] is None:
                    conn.execute((ROOT / "migrations" / migration).read_text(encoding="utf-8"))
            conn.execute("TRUNCATE ops.sources CASCADE")
        from test_texas_civix import TexasCivixTests

        legacy = TexasCivixTests()
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        old_report = legacy.parse()
        old_load = load_parsed(
            legacy.source, legacy.settings, legacy.root, Path(old_report["output_dir"]), db
        )
        original = publication.publish(ROOT, db, old_load["release_id"], "unofficial")
        for code, count in (("TX", 40), ("MN", 10), ("NC", 15), ("NE", 5)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, settings, parsed = parse_fixture(code, members(code), root)
                self.assertEqual(parsed["outcome"], "parsed", parsed)
                loaded = load_parsed(source, settings, root, Path(parsed["output_dir"]), db)
                self.assertEqual(loaded["outcome"], "succeeded", loaded)
                checked = publication.prepare(ROOT, db, loaded["release_id"], "unofficial")
                with connect(db) as conn:
                    self.assertIsNone(
                        conn.execute(
                            "SELECT * FROM serving.current_publications WHERE source_id=%s",
                            (source.source_id,),
                        ).fetchone()
                    )
                published = publication.publish_prepared(ROOT, db, checked["candidate_id"])
                self.assertEqual(published["outcome"], "published", published)
                with connect(db) as conn:
                    old = conn.execute(
                        "SELECT publication_id FROM serving.current_publications "
                        "WHERE source_id=%s",
                        (legacy.source.source_id,),
                    ).fetchone()
                    self.assertEqual(str(old["publication_id"]), original["publication_id"])
                    conn.execute("SET ROLE election_dashboard")
                    rows = conn.execute(
                        f"SELECT * FROM {code.lower()}.contest_candidate_results "
                        "WHERE publication_id=%s",
                        (published["publication_id"],),
                    ).fetchall()
                    self.assertEqual(len({row["contest_id"] for row in rows}), count)
                    if code == "NC":
                        write_ins = [
                            row for row in rows if row["choice"] == "Write-In (Miscellaneous)"
                        ]
                        self.assertEqual(len(write_ins), 1)
                        self.assertIsNone(write_ins[0]["choice_party"])
                        self.assertEqual(write_ins[0]["total_votes"], 10)
                    self.assertFalse(
                        conn.execute(
                            "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                            (code.lower() + ".results",),
                        ).fetchone()["ok"]
                    )


if __name__ == "__main__":
    unittest.main()
