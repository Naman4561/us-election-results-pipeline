"""Recorded official responses, adversarial mutations and isolated publication checks."""

import csv
import gzip
import hashlib
import html
import io
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path

from election_pipeline.collectors.clarity import version_base
from election_pipeline.collectors.sc import latest_report
from election_pipeline.parsers.html import Tree
from election_pipeline.parsers.structured import xml
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/five_states"
MEMBERS = {
    # The official results of November 20 and their ballot items (county detail).
    "GA": {
        "results.json": "ga-official-results.json",
        **{
            f"item-ga-{race}.json": f"ga-item-ga-{race}.json"
            for race in ("sen", "gov", *(f"{n:02}" for n in range(1, 15)))
        },
    },
    "SC": {
        "current_ver.txt": "sc-current_ver.txt",
        "summary.json": "sc-summary.json",
        "settings.json": "sc-settings.json",
        "detail.zip": "sc-detail.zip",
    },
    # Snapshots taken before SC moved to its Clarity site.
    "SC_WORKBOOK": {"index.html": "sc-general.html", "results.xlsx": "sc-results.xlsx"},
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
        self.settings, self.sources = load_config(ROOT / "config")

    def members(self, code):
        return {
            name: gzip.decompress((FIXTURES / (fixture + ".gz")).read_bytes())
            for name, fixture in MEMBERS[code].items()
        }

    def parse(self, code, mutate=None):
        self.source = self.sources[code[:2].lower() + "_2026_general"]
        if code == "SC_WORKBOOK":
            page = "https://scvotes.gov/2026-general-election-results/"
            self.source = replace(
                self.source,
                url=page,
                primary_filename="results.xlsx",
                expected_members=("index.html", "results.xlsx"),
                supplemental_members=(),
                files=(("index.html", page),),
            )
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

    def test_ga_and_sc_report_their_own_official_flags(self):
        self.assertIs(self.parse("GA")["official_results"], True)

        def unofficial(members):
            data = json.loads(members["results.json"])
            data["election"]["isOfficialResults"] = False
            members["results.json"] = json.dumps(data).encode()

        self.assertIs(self.parse("GA", unofficial)["official_results"], False)
        self.assertIs(self.parse("SC")["official_results"], False)

    def test_ga_wrong_election_missing_race_candidate_and_inconsistent_counties_rejected(self):
        def apply(edit, member="results.json"):
            def mutate(members):
                data = json.loads(members[member])
                edit(data)
                members[member] = json.dumps(data).encode()

            return mutate

        def county(d, change):
            locality = d["breakdownResults"][0]
            locality["ballotOptions"][0]["voteCount"] += change
            locality["voteTotal"] += change

        def drop(d):
            d["breakdownResults"].pop()
            d["breakdownResultCount"] -= 1

        def repeat(d):
            d["breakdownResults"].append(d["breakdownResults"][0])
            d["breakdownResultCount"] += 1

        edits = [
            (lambda d: d["election"].update(electionDate="2024-11-05"), "results.json"),
            (lambda d: d["ballotItems"].pop(0), "results.json"),
            (
                lambda d: d["ballotItems"][0]["summaryResults"]["ballotOptions"].pop(),
                "results.json",
            ),
            (
                lambda d: d["ballotItems"][0]["summaryResults"]["ballotOptions"][0].update(
                    voteCount=None
                ),
                "results.json",
            ),
            # A county that no longer adds up to the statewide count, one that is missing,
            # and one counted twice.
            (lambda d: county(d, 1), "item-ga-sen.json"),
            (drop, "item-ga-sen.json"),
            (repeat, "item-ga-gov.json"),
        ]
        for edit, member in edits:
            self.assertEqual(self.parse("GA", apply(edit, member))["outcome"], "validation_failed")

    def test_ga_duplicate_json_keys_and_captcha_rejected(self):
        for raw in [b'{"election":{},"election":{}}', b"<html><title>Captcha</title></html>"]:
            self.assertEqual(
                self.parse("GA", lambda m: m.update({"results.json": raw}))["outcome"],
                "validation_failed",
            )

    def test_ga_explicit_zero_snapshot_is_valid(self):
        def zero(value):
            if isinstance(value, dict):
                return {
                    k: 0 if k in ("voteCount", "voteTotal") else zero(v) for k, v in value.items()
                }
            return [zero(v) for v in value] if isinstance(value, list) else value

        def mutate(members):
            for name, body in members.items():
                members[name] = json.dumps(zero(json.loads(body))).encode()

        report = self.parse("GA", mutate)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertTrue(report["all_counts_zero"])

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

    def test_ks_unreported_counties_remain_absent(self):
        rows = self.read(self.parse("KS"), "county_results.csv")
        self.assertNotIn("BARBER", {r["county"] for r in rows if r["contest_id"] == "KS-SEN"})

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

    def test_oh_explicit_zero_snapshot_is_valid(self):
        def mutate(m):
            root = xml(m["results.xml"])
            for contest in root.findall("Election/ContestCollection/Contest"):
                for v in contest.findall("BallotSelection/VoteCountsCollection/VoteCounts"):
                    v.set("Count", "0")
            m["results.xml"] = ET.tostring(root)

        report = self.parse("OH", mutate)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertTrue(report["all_counts_zero"])

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

    def test_sc_clarity_methods_counties_and_election_must_agree(self):
        report = self.parse("SC")
        self.assertEqual(report["vote_method_reconciliation"], "passed")
        self.assertEqual(report["source_update_text"], "11/6/2026 6:26:12 PM EST")
        totals = {
            r["choice"]: r["total_votes"]
            for r in self.read(report, "contest_results.csv")
            if r["contest_id"] == "SC-GOV"
        }
        self.assertEqual(totals["Alan Wilson / Mike Reichenbach"], 1051010)

        def detail(edit):
            def mutate(members):
                with zipfile.ZipFile(io.BytesIO(members["detail.zip"])) as z:
                    root = ET.fromstring(z.read("detail.xml"))
                edit(root)
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("detail.xml", ET.tostring(root))
                members["detail.zip"] = out.getvalue()

            return mutate

        def governor(root):
            return next(c for c in root.findall("Contest") if c.get("key") == "21500")

        def method_vote(root):
            county = governor(root).find("Choice/VoteType/County")
            county.set("votes", str(int(county.get("votes")) + 1))

        def missing_county(root):
            kind = governor(root).find("Choice/VoteType")
            kind.remove(kind.find("County"))

        def renamed_method(root):
            governor(root).find("Choice/VoteType").set("name", "Mail")

        def election(root):
            root.find("ElectionName").text = "2024 Statewide General Election"

        for edit in [method_vote, missing_county, renamed_method, election]:
            with self.subTest(edit=edit.__name__):
                self.assertEqual(self.parse("SC", detail(edit))["outcome"], "validation_failed")
        self.assertEqual(
            version_base("https://www.enr-scvotes.org/SC/127300/current_ver.txt", b"386034\n"),
            "https://www.enr-scvotes.org/SC/127300/386034/",
        )
        with self.assertRaises(ValueError):
            version_base("https://enr-scvotes.example/SC/127300/current_ver.txt", b"1")

    def test_sc_workbook_snapshots_from_before_the_switch_still_parse(self):
        report = self.parse("SC_WORKBOOK")
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual((report["output_rows"], report["contest_count"]), (687, 9))
        self.assertEqual(report["county_reconciliation"], "passed")
        self.assertEqual(report["vote_method_reconciliation"], "passed")

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
            self.assertEqual(self.parse("SC_WORKBOOK", apply(edit))["outcome"], "validation_failed")

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

        report = self.parse("SC_WORKBOOK", mutate)
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertTrue(report["all_counts_zero"])
