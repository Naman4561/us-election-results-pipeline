"""Derived from the operator's version-9 summary; not a full original feed capture."""

import base64
import copy
import csv
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from election_pipeline.dashboard.server import STATIC, make_payload
from election_pipeline.database import converted_rows, verify
from election_pipeline.deployment import catalog, containers, control
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    observed = {
        "U. S. SENATOR ": [
            (44430, "AVERY AYERS", "W", 51),
            (33528, "TED BROWN", "LIB", 3233),
            (44432, 'BRADFORD "BRAD" HALL', "W", 40),
            (44434, "ZUL MOHAMED", "W", 0),
            (42424, "KEN PAXTON", "REP", 95047),
            (44534, "JADE SIMMONS", "W", 18),
            (41261, "JAMES TALARICO", "DEM", 55172),
        ],
        "GOVERNOR": [
            (39537, "GREG ABBOTT (I)", "REP", 94229),
            (33621, "PAT DIXON", "LIB", 1787),
            (44433, "EDWARD EARL GILES", "W", 18),
            (41284, "GINA HINOJOSA", "DEM", 38161),
            (43373, "JENN MACK", "W", 38),
            (44429, "JINGHPAW MAJOISHINGRA", "W", 0),
            (44431, "MIKE MILLER", "W", 32),
        ],
        "U. S. REPRESENTATIVE DISTRICT 15": [
            (39524, "MONICA DE LA CRUZ (I)", "REP", 0),
            (41270, "BOBBY PULIDO", "DEM", 0),
        ],
        "U. S. REPRESENTATIVE DISTRICT 23": [
            (42864, "BRANDON HERRERA", "REP", 0),
            (42976, "BEN MENDOZA", "IND", 0),
            (41274, "KATY PADILLA STOUT", "DEM", 0),
        ],
        "U. S. REPRESENTATIVE DISTRICT 28": [
            (41278, "HENRY CUELLAR (I)", "DEM", 16152),
            (42154, "MARLÓN DURÁN", "GRE", 488),
            (39533, "TANO E. TIJERINA", "REP", 3800),
        ],
        "U. S. REPRESENTATIVE DISTRICT 34": [
            (42188, "EDDIE ESPINOZA", "GRE", 0),
            (39535, "ERIC FLORES", "REP", 0),
            (41282, "VICENTE GONZALEZ (I)", "DEM", 0),
            (33606, "CHRIS ROYAL", "LIB", 0),
        ],
        "U. S. REPRESENTATIVE DISTRICT 35": [
            (42429, "CARLOS DE LA CRUZ", "REP", 0),
            (42609, "JOHNNY C. GARCIA", "DEM", 0),
        ],
    }
    federal, statewide = [], []
    for fake_id, (name, candidates) in enumerate(observed.items(), 1):
        # Original race IDs were not in the pasted summary; these IDs are synthetic.
        race = {
            "id": fake_id,
            "N": name,
            "T": sum(c[3] for c in candidates),
            "Candidates": [dict(zip(("ID", "N", "P", "V"), candidate)) for candidate in candidates],
        }
        (statewide if name == "GOVERNOR" else federal).append(race)
    return {
        "Version": "enr/53815/9/",
        "Home": {"ElecDate": "11032026", "LastUpdatedTime": "Nov 04, 2026 01:28:46"},
        "Federal": {"OffType": "FEDERAL OFFICES", "OffTypeID": 510, "Races": federal},
        "StateWide": {"OffType": "STATEWIDE OFFICES", "OffTypeID": 511, "Races": statewide},
    }


class TexasCivixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["tx_2026_general"]

    def parse(self, mutate=None, raw=None):
        data = fixture()
        if mutate:
            mutate(data)
        envelope = {
            k: v if k == "Version" else base64.b64encode(json.dumps(v).encode()).decode()
            for k, v in data.items()
        }
        archive = self.root / "input.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("results.json", raw or json.dumps(envelope).encode())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        archive.replace(target)
        return parse_source(self.source, self.settings, self.root, sha256=digest)

    def test_observed_vote_totals_and_unicode_names_round_trip(self):
        result = self.parse()
        self.assertEqual(result["outcome"], "parsed", result)
        self.assertEqual(result["source_version"], "enr/53815/9/")
        self.assertEqual(result["contest_total_reconciliation"], "passed")
        self.assertEqual(result["contest_count"], 7)
        self.assertFalse(result["all_counts_zero"])
        with (Path(result["output_dir"]) / "contest_results.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        totals = {}
        for row in rows:
            totals[row["contest_id"]] = totals.get(row["contest_id"], 0) + int(row["total_votes"])
        self.assertEqual(
            totals,
            {
                "TX-SEN": 153561,
                "TX-GOV": 134265,
                "TX-28": 20440,
                "TX-15": 0,
                "TX-23": 0,
                "TX-34": 0,
                "TX-35": 0,
            },
        )
        self.assertTrue(any(row["choice"] == "MARLÓN DURÁN" for row in rows))
        self.assertEqual(sum(row["choice_party"] == "W" for row in rows), 8)
        store = ArchiveStore(self.root, self.source)
        verify(self.source, self.settings, store, Path(result["output_dir"]))
        contract = control.state_adapter("TX").contract("election_results")
        converted = list(
            converted_rows(
                Path(result["output_dir"]) / "results.csv", contract.fields, self.source, 99, 28
            )
        )
        self.assertEqual(len(converted), 28)
        self.assertTrue(all(row[0] == 99 for row in converted))
        for row in rows:
            row["total_votes"] = int(row["total_votes"])
        payload = make_payload(
            json.loads((STATIC / "tx-map.json").read_text(encoding="utf-8")),
            {"status": "unofficial"},
            [],
            rows,
            state="TX",
        )
        self.assertEqual(payload["races"]["TX-SEN"]["stats"]["total_votes"]["total"], 153561)
        self.assertEqual(payload["races"]["TX-15"]["stats"]["total_votes"]["status"], "unreported")

    def test_wrong_election_and_malformed_metadata_are_rejected(self):
        changes = [
            lambda d: d.update(Version="enr/53814/9/"),
            lambda d: d.update(Version="invalid"),
            lambda d: d["Home"].update(ElecDate="11032024"),
            lambda d: d["Home"].update(ElecDate="1132026"),
            lambda d: d["Home"].update(LastUpdatedTime="yesterday"),
            lambda d: d["Federal"].update(OffType="STATEWIDE OFFICES"),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.parse(change)["outcome"], "validation_failed")

    def test_missing_duplicate_and_wrong_section_races_are_rejected(self):
        def duplicate(data):
            data["Federal"]["Races"].append(copy.deepcopy(data["Federal"]["Races"][0]))

        def wrong_section(data):
            data["Federal"]["Races"].extend(data["StateWide"]["Races"])
            data["StateWide"]["Races"] = []

        for change in (lambda d: d["StateWide"].update(Races=[]), duplicate, wrong_section):
            self.assertEqual(self.parse(change)["outcome"], "validation_failed")

    def test_bad_votes_and_inconsistent_totals_are_rejected(self):
        for value in (None, "", "51", True, -1, 1.5, 9223372036854775808):
            with self.subTest(value=value):
                result = self.parse(
                    lambda d: d["Federal"]["Races"][0]["Candidates"][0].update(V=value)
                )
                self.assertEqual(result["outcome"], "validation_failed")
        self.assertEqual(
            self.parse(lambda d: d["Federal"]["Races"][0].update(T=1))["outcome"],
            "validation_failed",
        )

    def test_duplicate_candidates_are_rejected_even_when_totals_match(self):
        def duplicate(data):
            race = data["Federal"]["Races"][0]
            race["Candidates"].append(copy.deepcopy(race["Candidates"][0]))
            race["T"] += race["Candidates"][0]["V"]

        self.assertEqual(self.parse(duplicate)["outcome"], "validation_failed")

    def test_zero_snapshot_and_downward_correction_remain_valid(self):
        def zero(data):
            for section in ("Federal", "StateWide"):
                for race in data[section]["Races"]:
                    race["T"] = 0
                    for candidate in race["Candidates"]:
                        candidate["V"] = 0

        result = self.parse(zero)
        self.assertEqual(result["outcome"], "parsed", result)
        self.assertTrue(result["all_counts_zero"])

        def correction(data):
            data["Version"] = "enr/53815/10/"
            data["Federal"]["Races"][0]["Candidates"][4]["V"] -= 100
            data["Federal"]["Races"][0]["T"] -= 100

        self.assertEqual(self.parse(correction)["outcome"], "parsed")

    def test_invalid_envelope_json_base64_and_missing_sections_fail(self):
        for raw in (
            b"<html>blocked</html>",
            b'{"Version":"a","Version":"b"}',
            b'{"Version":NaN}',
            b'{"Version":"enr/53815/9/","Home":"!!!!"}',
        ):
            self.assertEqual(self.parse(raw=raw)["outcome"], "validation_failed")


class SourceTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "catalog.json"
        settings, sources = load_config(ROOT / "config/sources.toml")
        self.source, self.settings = sources["tx_2026_general"], settings
        self.name = self.source.source_id
        old_source = replace(
            self.source,
            url="https://electionresults.sos.state.tx.us/results.html",
            primary_filename="results.html",
            expected_members=("results.html",),
        )
        self.old_image, self.new_image, self.revision = (
            "sha256:" + "a" * 64,
            "sha256:" + "c" * 64,
            "b" * 40,
        )

        def entry(source):
            result = catalog.entry(
                source,
                settings,
                image_id=self.old_image,
                revision=self.revision,
                release_path=self.root,
                worker="baseline",
                status="test",
            )
            result["races"] = list(control.state_adapter(source.state).races)
            return result

        self.value = {
            "version": 1,
            "generation": 0,
            "baseline": {
                "image_id": self.old_image,
                "revision": self.revision,
                "source_ids": ["nc_2026_general", self.name],
            },
            "sources": {
                "nc_2026_general": entry(sources["nc_2026_general"]),
                self.name: entry(old_source),
            },
        }
        catalog.write(self.path, self.value)
        self.prod_file = self.root / "production.env"
        self.original = (
            f"IMAGE_TAG={self.revision}\nPIPELINE_STATUS=unofficial\n"
            f"PIPELINE_SOURCES=nc_2026_general,{self.name}\n"
            f"DATA_DIR={self.root}/data\nPOSTGRES_PASSWORD=keep-secret\n"
        )
        self.prod_file.write_text(self.original, encoding="utf-8")
        self.evidence = self.root / "response.json"
        self.evidence.write_text(json.dumps(fixture()), encoding="utf-8")
        self.report = {
            "outcome": "parsed",
            "source_id": self.name,
            "source_signature": self.source.signature(settings),
            "collector_code_sha256": "code-hash",
            "internal_reconciliation": "passed",
            "contest_total_reconciliation": "passed",
            "contest_count": 7,
            "all_counts_zero": False,
            "raw_sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.report_file = self.root / "check.json"
        self.report_file.write_text(json.dumps(self.report), encoding="utf-8")
        self.config = {
            "PRODUCTION_ENV": str(self.prod_file),
            "CONTROL_STATE_DIR": str(self.root),
            "EXPANSION_CPUS": "0.5",
            "EXPANSION_MEMORY": "768m",
        }
        self.meta = {
            "source": asdict(self.source),
            "settings": asdict(settings),
            "code_sha256": "code-hash",
            "races": self.value["sources"][self.name]["races"],
        }

    def transfer(self, *, states=("inactive", "inactive")):
        with (
            patch.object(control, "controls_ready"),
            patch.object(containers, "verify_release", return_value=(self.new_image, "d" * 40)),
            patch.object(containers, "metadata", return_value=self.meta),
            patch.object(containers, "package_hash", return_value="code-hash"),
            patch.object(containers, "release_hash", return_value="e" * 64),
            patch.object(containers, "command", side_effect=states) as command,
        ):
            result = control.transfer_source(
                self.path,
                self.root,
                "candidate-image",
                self.name,
                "unofficial",
                self.evidence,
                self.report_file,
                self.config,
            )
            self.commands = command.call_args_list
            return result

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_transfer_preserves_other_state_and_image_and_changes_only_source_selection(self):
        result = self.transfer()
        value = catalog.read(self.path)
        self.assertEqual(result["outcome"], "transferred")
        self.assertEqual(
            value["sources"]["nc_2026_general"],
            json.loads(json.dumps(self.value["sources"]["nc_2026_general"])),
        )
        self.assertEqual(value["baseline"]["image_id"], self.old_image)
        prod = dict(dotenv_values(self.prod_file))
        self.assertEqual(prod["IMAGE_TAG"], self.revision)
        self.assertEqual(prod["POSTGRES_PASSWORD"], "keep-secret")
        self.assertEqual(prod["PIPELINE_SOURCES"], "nc_2026_general")
        self.assertEqual(value["sources"][self.name]["worker"], "expansion")
        self.assertTrue(value["sources"][self.name]["public"])
        self.assertTrue(value["sources"][self.name]["enabled"])
        self.assertEqual(value["sources"][self.name]["status"], "unofficial")
        catalog.verify_baseline(value, prod)
        self.assertTrue(all(c.args[0][:2] == ["systemctl", "show"] for c in self.commands))

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_active_baseline_cycle_prevents_mutation(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "cycle is running"):
            self.transfer(states=("activating",))
        self.assertEqual(self.prod_file.read_text(), self.original)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_cycle_starting_during_transfer_restores_selection(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "cycle is running"):
            self.transfer(states=("inactive", "activating"))
        self.assertEqual(self.prod_file.read_text(), self.original)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            json.loads((self.root / "source-handoff.json").read_text())["outcome"], "rolled_back"
        )

    @unittest.skipIf(os.name == "nt", "Linux host catalog lock")
    def test_catalog_write_failure_restores_original_worker_configuration(self):
        real_write = catalog.write

        def write(path, value):
            if value["generation"] == 1:
                raise OSError("disk error")
            return real_write(path, value)

        with patch.object(catalog, "write", side_effect=write):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.transfer()
        self.assertEqual(self.prod_file.read_text(), self.original)
        self.assertEqual(catalog.read(self.path)["generation"], 0)

    def test_wrong_or_tampered_validation_evidence_fails_before_transfer(self):
        self.evidence.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "matching successful"):
            self.transfer()
        self.assertEqual(self.prod_file.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
