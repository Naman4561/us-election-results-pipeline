"""Recorded official responses, adversarial mutations and isolated publication checks."""

import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from loading import copies

from election_pipeline import bundles, publication
from election_pipeline.collect import FetchFailure, FetchResponse
from election_pipeline.collectors.sc import latest_report
from election_pipeline.dashboard.server import STATIC, make_payload, national_payload
from election_pipeline.database import (
    connect,
    connection_settings,
    load_parsed,
)
from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.structured import xml
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/five_states"
CODES = ("GA", "SC", "KS", "OH")
MEMBERS = {
    "GA": {"results.json": "ga-results.json"},
    "SC": {"index.html": "sc-general.html", "results.xlsx": "sc-results.xlsx"},
    "KS": {"results.html": "ks.html"},
    "OH": {"results.xml": "oh-results.xml"},
}


def render(node):
    if isinstance(node, str):
        return html.escape(node)
    inside = "".join(render(c) for c in node.children)
    if node.tag == "root":
        return inside
    attrs = "".join(
        " " + k + ('="' + html.escape(v, quote=True) + '"' if v is not None else "")
        for k, v in node.attrs.items()
    )
    return "<" + node.tag + attrs + ">" + inside + "</" + node.tag + ">"


def edit_xml_member(raw, name, mutate):
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as original,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == name:
                tree = xml(data)
                mutate(tree)
                # The feed parser ignores namespace prefixes, not the workbook reader's contract.
                ET.register_namespace(
                    "", "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                )
                for n in tree.iter():
                    n.tag = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}" + n.tag
                data = ET.tostring(tree, encoding="utf-8")
            target.writestr(item.filename, data)
    return out.getvalue()


class FiveStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, self.sources = load_config(ROOT / "config/sources.toml")

    def members(self, code):
        return {
            name: gzip.decompress((FIXTURES / (fixture + ".gz")).read_bytes())
            for name, fixture in MEMBERS[code].items()
        }

    def parse(self, code, mutate=None):
        self.source = self.sources[code.lower() + "_2026_general"]
        members = self.members(code)
        if mutate:
            mutate(members)
        raw = self.root / "input.zip"
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        self.store = ArchiveStore(self.root, self.source)
        target = self.store.archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            row["total_votes"] = int(row["total_votes"]) if row["total_votes"] else None
        return rows

    def test_official_snapshots_parse_verify_reconcile_and_convert(self):
        for code, records, contests in [
            ("GA", 32, 16),
            ("SC", 687, 9),
            ("KS", 522, 6),
            ("OH", 1475, 17),
        ]:
            with self.subTest(code=code):
                report = self.parse(code)
                self.assertEqual(report["outcome"], "parsed", report)
                self.assertEqual(report["output_rows"], records)
                self.assertEqual(report["contest_count"], contests)
                self.assertFalse(report["all_counts_zero"])
                self.assertEqual(
                    report["county_reconciliation"], "not_available" if code == "GA" else "passed"
                )
                directory = Path(report["output_dir"])
                converted = copies(self.source, self.settings, self.store, directory, 88)[
                    ("landing", "results")
                ]
                self.assertEqual(len(converted), records)
                self.assertEqual(converted[0][:2], (88, 2))
                replay = parse_source(
                    self.source, self.settings, self.root, sha256=report["identity"]["raw_sha256"]
                )
                self.assertEqual(replay["outcome"], "already_parsed")

    def test_fixture_provenance_and_scope_match_configured_races(self):
        ledger = json.loads((FIXTURES / "provenance.json").read_text())
        for name, metadata in ledger.items():
            raw = gzip.decompress((FIXTURES / (name + ".gz")).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), metadata["sha256"])
            self.assertEqual(len(raw), metadata["bytes"])
        self.assertNotIn("me_2026_general", self.sources)

    def test_maps_and_dashboard_match_all_new_state_results(self):
        providers = {}
        for code, counties, districts in [
            ("GA", 159, 14),
            ("SC", 46, 7),
            ("KS", 105, 4),
            ("OH", 88, 15),
        ]:
            geography = json.loads((STATIC / (code.lower() + "-map.json")).read_text())
            self.assertEqual(len(geography["counties"]), counties)
            self.assertEqual(len(geography["districts"]), districts)
            report = self.parse(code)
            payload = make_payload(
                geography,
                {"status": "unofficial"},
                self.read(report, "county_results.csv"),
                self.read(report, "contest_results.csv"),
                state=code,
            )
            self.assertEqual(set(payload["races"]), set(state_adapter(code).races))
            self.assertEqual(payload["county_detail"], code != "GA")
            self.assertGreater(payload["races"][code + "-SEN"]["stats"]["total_votes"]["total"], 0)
            providers[code] = lambda payload=payload: payload
        national = national_payload(providers)
        self.assertEqual(set(national["states"]), set(CODES))
        self.assertNotIn("ME", national["states"])

    def test_ga_wrong_election_missing_race_candidate_and_inconsistent_methods_rejected(self):
        def apply(edit):
            def mutate(members):
                data = json.loads(members["results.json"])
                edit(data)
                members["results.json"] = json.dumps(data).encode()

            return mutate

        edits = [
            lambda d: d["election"].update(electionDate="2024-11-05"),
            lambda d: d["ballotItems"].pop(0),
            lambda d: d["ballotItems"][0]["summaryResults"]["ballotOptions"].pop(),
            lambda d: d["ballotItems"][0]["summaryResults"]["ballotOptions"][0].update(
                voteCount=None
            ),
            lambda d: d["ballotItems"][0]["summaryResults"]["ballotOptions"][0]["groupResults"][
                0
            ].update(voteCount=1),
        ]
        for edit in edits:
            self.assertEqual(self.parse("GA", apply(edit))["outcome"], "validation_failed")

    def test_ga_duplicate_json_keys_and_captcha_rejected(self):
        for raw in [b'{"election":{},"election":{}}', b"<html><title>Captcha</title></html>"]:
            self.assertEqual(
                self.parse("GA", lambda m: m.update({"results.json": raw}))["outcome"],
                "validation_failed",
            )

    def test_ga_zero_and_downward_correction_are_valid(self):
        for zero in (True, False):

            def mutate(members):
                d = json.loads(members["results.json"])
                for b in d["ballotItems"]:
                    for c in b["summaryResults"]["ballotOptions"]:
                        if zero:
                            c["voteCount"] = 0
                            for g in c["groupResults"]:
                                g["voteCount"] = 0
                        elif c["voteCount"] and c["groupResults"]:
                            g = next(g for g in c["groupResults"] if g["voteCount"])
                            g["voteCount"] -= 1
                            c["voteCount"] -= 1
                    b["voteTotal"] = sum(
                        c["voteCount"] for c in b["summaryResults"]["ballotOptions"]
                    )
                members["results.json"] = json.dumps(d).encode()

            report = self.parse("GA", mutate)
            self.assertEqual(report["outcome"], "parsed", report)
            self.assertEqual(report["all_counts_zero"], zero)

    def test_ks_missing_unreported_county_and_altered_votes_rejected(self):
        def missing(members):
            tree = Tree(members["results.html"].decode()).root
            block = tree.find(identity="party-all")[0].find("div", css="race")[0]
            for area in block.find("area"):
                if "No results reported" in area.attrs["title"]:
                    area.attrs["alt"] = "DUPLICATE"
                    break
            members["results.html"] = render(tree).encode()

        self.assertEqual(self.parse("KS", missing)["outcome"], "validation_failed")
        for old, new in [
            (b"383,781", b"383,780"),
            (b"2026 General Election Results", b"2024 General Election Results"),
            (b"665,532", b"665,533"),
        ]:
            self.assertEqual(
                self.parse(
                    "KS", lambda m: m.update({"results.html": m["results.html"].replace(old, new)})
                )["outcome"],
                "validation_failed",
            )

    def test_ks_unreported_counties_remain_absent_and_downward_correction_allowed(self):
        original = self.parse("KS")
        rows = self.read(original, "county_results.csv")
        self.assertNotIn("BARBER", {r["county"] for r in rows if r["contest_id"] == "KS-SEN"})

        def mutate(m):
            raw = m["results.html"].decode()
            raw = raw.replace("383,781", "383,780").replace("665,532", "665,531")
            raw = raw.replace(
                "D-Adam Hamilton</td><td style='text-align:right'>535</td>",
                "D-Adam Hamilton</td><td style='text-align:right'>534</td>",
            )
            m["results.html"] = raw.encode()

        corrected = self.parse("KS", mutate)
        self.assertEqual(corrected["outcome"], "parsed", corrected)

    def test_ks_explicit_numeric_zero_snapshot_is_valid(self):
        def zero(members):
            raw = members["results.html"].decode("utf-8")
            members["results.html"] = re.sub(r"(?<=>)[0-9][0-9,]*(?=</(?:p|td)>)", "0", raw).encode(
                "utf-8"
            )

        report = self.parse("KS", zero)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertTrue(report["all_counts_zero"])

    def test_oh_joint_ticket_is_counted_once_and_all_counties_reconcile(self):
        report = self.parse("OH")
        rows = self.read(report, "contest_results.csv")
        acton = [r for r in rows if r["contest_id"] == "OH-GOV" and r["choice"] == "Amy Acton"]
        self.assertEqual(len(acton), 1)
        self.assertEqual(acton[0]["total_votes"], 1502912)
        self.assertEqual(report["county_counts"]["OH-SEN"], 88)
        self.assertEqual(report["county_counts"]["OH-GOV"], 88)

    def test_oh_missing_zero_county_reference_bad_votes_duplicate_and_date_rejected(self):
        def apply(edit):
            def mutate(m):
                root = xml(m["results.xml"])
                edit(root)
                m["results.xml"] = ET.tostring(root)

            return mutate

        def missing(root):
            c = next(
                c
                for c in root.findall("Election/ContestCollection/Contest")
                if c.get("Name") == "U.S. Senator"
            )
            chosen = c.find("BallotSelection/VoteCountsCollection/VoteCounts/GpUnitId").text
            # Remove the same county from every candidate and adjust official totals too.
            selections = c.findall("BallotSelection")
            target = (
                selections[0].findall("VoteCountsCollection/VoteCounts")[1].findtext("GpUnitId")
            )
            for s in selections:
                group = s.find("VoteCountsCollection")
                official = next(v for v in group if v.findtext("GpUnitId") == chosen)
                county = next(v for v in group if v.findtext("GpUnitId") == target)
                official.set("Count", str(int(official.get("Count")) - int(county.get("Count"))))
                group.remove(county)

        edits = [
            missing,
            lambda r: r.find("Election").set("Date", "2024-11-05"),
            lambda r: r.find(
                "Election/ContestCollection/Contest/BallotSelection/VoteCountsCollection/VoteCounts"
            ).set("Count", "-1"),
            lambda r: r.find("Election/ContestCollection/Contest").append(
                r.find("Election/ContestCollection/Contest/BallotSelection")
            ),
        ]
        for edit in edits:
            self.assertEqual(self.parse("OH", apply(edit))["outcome"], "validation_failed")
        for raw in [b"<broken", b'<!DOCTYPE x [<!ENTITY y "1">]><ElectionReport/>']:
            self.assertEqual(
                self.parse("OH", lambda m: m.update({"results.xml": raw}))["outcome"],
                "validation_failed",
            )

    def test_oh_zero_and_downward_correction_are_valid(self):
        for zero in (True, False):

            def mutate(m):
                root = xml(m["results.xml"])
                for c in root.findall("Election/ContestCollection/Contest"):
                    for s in c.findall("BallotSelection"):
                        values = s.findall("VoteCountsCollection/VoteCounts")
                        if zero:
                            for v in values:
                                v.set("Count", "0")
                        elif values and int(values[0].get("Count")) > 0:
                            county = next(v for v in values[1:] if int(v.get("Count")) > 0)
                            county.set("Count", str(int(county.get("Count")) - 1))
                            values[0].set("Count", str(int(values[0].get("Count")) - 1))
                m["results.xml"] = ET.tostring(root)

            report = self.parse("OH", mutate)
            self.assertEqual(report["outcome"], "parsed", report)
            self.assertEqual(report["all_counts_zero"], zero)

    def test_sc_latest_explicit_link_selected_and_other_hosts_rejected(self):
        src = self.sources["sc_2026_general"]
        base = "https://scvotes.gov/wp-content/uploads/2026/11/2026-11-03-"
        first = base + "930-PM-General-Election-Results-.xlsx"
        second = base + "1030-PM-General-Election-Results-.xlsx"
        index = f'<a href="{second}">later</a><a href="{first}">earlier</a>'
        self.assertEqual(latest_report(index, src), second)
        for bad in [
            first.replace("scvotes.gov", "evil.example"),
            first.replace("2026-11-03", "2024-11-05"),
            first + "?redirect=1",
        ]:
            with self.assertRaises(ValueError):
                latest_report(f'<a href="{bad}">file</a>', src)
        with self.assertRaises(ValueError):
            latest_report("<title>Captcha</title>", src)

    def test_sc_bundle_preserves_index_and_resolves_only_linked_report_under_shared_budget(self):
        src = self.sources["sc_2026_general"]
        members = self.members("SC")
        calls = []

        def fetch(source, settings, path, headers, *, url=None):
            calls.append((url, settings.max_download_bytes))
            body = members["index.html"] if len(calls) == 1 else members["results.xlsx"]
            path.write_bytes(body)
            return FetchResponse(200, {}, url, len(body), hashlib.sha256(body).hexdigest())

        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            result = bundles.fetch_bundle(src, self.settings, self.root / "bundle.zip")
        self.assertTrue(calls[1][0].endswith("930-PM-General-Election-Results-.xlsx"))
        self.assertEqual(calls[1][1], self.settings.max_download_bytes - len(members["index.html"]))
        self.assertEqual(result.components[1]["url"], calls[1][0])
        with zipfile.ZipFile(self.root / "bundle.zip") as z:
            self.assertEqual(z.read("index.html"), members["index.html"])
            self.assertEqual(z.read("results.xlsx"), members["results.xlsx"])
        with patch("election_pipeline.collectors.common.fetch_file", side_effect=fetch):
            calls.clear()
            with self.assertRaises(FetchFailure):
                bundles.fetch_bundle(
                    src, replace(self.settings, max_download_bytes=10), self.root / "small.zip"
                )

    def test_sc_method_mismatch_missing_county_and_formula_rejected(self):
        def apply(edit):
            return lambda m: m.update(
                {
                    "results.xlsx": edit_xml_member(
                        m["results.xlsx"], "xl/worksheets/sheet3.xml", edit
                    )
                }
            )

        def formula(root):
            cell = root.find("sheetData/row/c")
            ET.SubElement(cell, "f").text = "1+1"

        def vote(root):
            cell = next(c for c in root.findall("sheetData/row/c") if c.get("r") == "C4")
            cell.find("v").text = "52"

        def missing(root):
            rows = root.find("sheetData")
            rows.remove(next(r for r in rows if r.get("r") == "4"))

        for edit in [formula, vote, missing]:
            self.assertEqual(self.parse("SC", apply(edit))["outcome"], "validation_failed")

    def test_sc_downward_correction_and_county_method_totals_remain_valid(self):
        def edit(root):
            last = max(int(r.get("r")) for r in root.findall("sheetData/row"))
            for c in root.findall("sheetData/row/c"):
                if c.get("r") in {f"{col}{r}" for col in ("C", "I", "AL") for r in (4, last)}:
                    v = c.find("v")
                    v.text = str(int(v.text) - 1)

        report = self.parse(
            "SC",
            lambda m: m.update(
                {
                    "results.xlsx": edit_xml_member(
                        m["results.xlsx"], "xl/worksheets/sheet3.xml", edit
                    )
                }
            ),
        )
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["vote_method_reconciliation"], "passed")

    def test_sc_explicit_numeric_zero_snapshot_is_valid(self):
        def zero(root):
            for cell in root.findall("sheetData/row/c"):
                column = re.match(r"[A-Z]+", cell.get("r"))[0]
                value = cell.find("v")
                if column not in {"A", "B"} and cell.get("t") in (None, "n") and value is not None:
                    value.text = "0"

        def mutate(members):
            for number in [3, *range(10, 18)]:
                members["results.xlsx"] = edit_xml_member(
                    members["results.xlsx"], f"xl/worksheets/sheet{number}.xml", zero
                )

        report = self.parse("SC", mutate)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertTrue(report["all_counts_zero"])


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class FiveStateDatabaseTests(unittest.TestCase):
    def test_four_states_load_validate_publish_replay_and_enforce_reader_permissions(self):
        name = os.environ["ELECTION_TEST_DATABASE"]
        if not name.endswith("_test"):
            raise ValueError("Disposable *_test database required")
        db = {**connection_settings(), "dbname": name}
        with connect(db) as conn:
            for path in sorted((ROOT / "migrations").glob("*.sql")):
                schema = {
                    "001_initial.sql": "ops.sources",
                    "002_publication.sql": "serving.publications",
                    "003_state_schemas.sql": "ky.results",
                    "004_mn_ne_tx.sql": "tx.results",
                }.get(path.name, f'"{path.stem[4:]}".results')  # "or" is reserved
                if conn.execute("SELECT to_regclass(%s) AS t", (schema,)).fetchone()["t"] is None:
                    conn.execute(path.read_text())
            conn.execute("TRUNCATE ops.sources CASCADE")
        for code in CODES:
            f = FiveStateTests()
            f.setUp()
            self.addCleanup(f.doCleanups)
            report = f.parse(code)
            loaded = load_parsed(f.source, f.settings, f.root, Path(report["output_dir"]), db)
            self.assertEqual(loaded["outcome"], "succeeded", loaded)
            candidate = publication.prepare(ROOT, db, loaded["release_id"], "test")
            self.assertEqual(candidate["outcome"], "validated", candidate)
            self.assertEqual(
                publication.publish_prepared(ROOT, db, candidate["candidate_id"])["outcome"],
                "published",
            )
            self.assertEqual(
                publication.publish(ROOT, db, loaded["release_id"], "test", skip_current=True)[
                    "outcome"
                ],
                "already_current",
            )
            with connect(db) as conn:
                conn.execute("SET ROLE election_dashboard")
                self.assertTrue(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (code.lower() + ".contest_candidate_results",),
                    ).fetchone()["ok"]
                )
                self.assertFalse(
                    conn.execute(
                        "SELECT has_table_privilege(current_user,%s,'SELECT') AS ok",
                        (code.lower() + ".results",),
                    ).fetchone()["ok"]
                )
