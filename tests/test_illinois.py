"""Illinois: election authorities' own results, collected county by county.

The fixture is an import of six Clarity authorities captured on November 23, 2026 (Lake,
McLean, Bloomington, Sangamon, Winnebago, Rockford; ``tests/fixtures/illinois``), which
complete four counties: McLean with Bloomington's city commission, and Winnebago with
Rockford's.
"""

import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from election_pipeline.parsers import il
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/illinois"


def members():
    with zipfile.ZipFile(FIXTURE / "results.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def manifest_of(data):
    return json.loads(data["manifest.json"])["authorities"]


def rewrite(data, member, body):
    """Replace one member and its SHA-256 in the manifest."""
    data[member] = body
    manifest = manifest_of(data)
    for entry in manifest.values():
        for part in entry["files"]:
            if part["file"] == member:
                part["sha256"] = hashlib.sha256(body).hexdigest()
    data["manifest.json"] = json.dumps({"authorities": manifest}).encode()


def summary_edit(member, change):
    def mutate(data):
        contests = json.loads(data[member])
        change(contests)
        rewrite(data, member, json.dumps(contests).encode())

    return mutate


def drop(authority):
    def mutate(data):
        manifest = manifest_of(data)
        for part in manifest.pop(authority)["files"]:
            data.pop(part["file"])
        data["manifest.json"] = json.dumps({"authorities": manifest}).encode()

    return mutate


class IllinoisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["il_2026_general"]

    def parse(self, mutate=None):
        data = members()
        if mutate:
            mutate(data)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in data.items():
                archive.writestr(name, value)
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("results.zip", inner.getvalue())
        sha = hashlib.sha256(outer.getvalue()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(outer.getvalue())
        return parse_source(self.source, self.settings, self.root, sha256=sha)

    def read(self, report, name):
        with (Path(report["output_dir"]) / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_the_fixture_is_the_captured_files(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())
        raw = (FIXTURE / "results.zip").read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ledger["results.zip"]["sha256"])
        data = members()
        for name, entry in ledger["members"].items():
            self.assertEqual(hashlib.sha256(data[name]).hexdigest(), entry["sha256"])
            self.assertTrue(entry["url"].startswith("https://results.enr.clarityelections.com/"))

    def test_authorities_add_up_by_county_with_partial_counts_marked(self):
        report = self.parse()
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertIn(
            "4 of 102 counties (6 of 108 election authorities", report["source_update_text"]
        )
        self.assertIn("partial counts: McLean, Winnebago", report["source_update_text"])
        counties = self.read(report, "county_results.csv")
        senate = {
            (r["county"], r["choice"]): int(r["total_votes"])
            for r in counties
            if r["contest_id"] == "IL-SEN"
        }
        # McLean is its county clerk's count and Bloomington's city commission's.
        self.assertEqual(senate[("McLean", "Juliana Stratton")], 20252 + 18197)
        self.assertEqual(senate[("Winnebago", "Don Tracy")], 26408 + 12183)
        self.assertEqual(senate[("Lake", "Juliana Stratton")], 155732)
        self.assertEqual({c for c, _ in senate}, {"Lake", "McLean", "Sangamon", "Winnebago"})
        # The partial count keeps its precincts: McLean's clerk 14 of 81, Bloomington 60 of 60.
        mclean = next(
            r
            for r in self.read(report, "results.csv")
            if r["county"] == "McLean" and r["contest_id"] == "IL-SEN"
        )
        self.assertEqual(
            (mclean["precincts_reporting"], mclean["precincts_total"]), (str(14 + 60), str(81 + 60))
        )
        statewide = {
            (r["contest_id"], r["choice"]): int(r["total_votes"])
            for r in self.read(report, "contest_results.csv")
        }
        self.assertEqual(statewide[("IL-17", "Write-in")], 17 + 5)
        self.assertEqual(
            statewide[("IL-GOV", "JB Pritzker / Christian Mitchell")],
            sum(
                int(r["total_votes"])
                for r in counties
                if r["contest_id"] == "IL-GOV" and r["choice"].startswith("JB Pritzker")
            ),
        )

    def test_a_county_waits_for_every_authority_in_it(self):
        report = self.parse(drop("Winnebago"))
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["counties_waiting"], ["Winnebago"])
        counties = {r["county"] for r in self.read(report, "county_results.csv")}
        self.assertEqual(counties, {"Lake", "McLean", "Sangamon"})

    def test_changed_counts_names_or_files_are_refused(self):
        def votes(contests):
            contests[0]["V"][0] += 1

        def name(contests):
            contests[0]["CH"][0] = "Donald Tracy"

        def precincts(contests):
            contests[0]["PR"] = contests[0]["TP"] + 1

        def unlisted(data):
            data["clarity/Lake.summary.json"] = data["clarity/Lake.summary.json"] + b" "

        def foreign(data):
            manifest = manifest_of(data)
            manifest["Peoria"] = manifest.pop("Sangamon")
            data["manifest.json"] = json.dumps({"authorities": manifest}).encode()

        changes = {
            "votes": summary_edit("clarity/Lake.summary.json", votes),
            "unknown name": summary_edit("clarity/Lake.summary.json", name),
            "precincts": summary_edit("clarity/Sangamon.summary.json", precincts),
            "file": unlisted,
            "wrong county": foreign,
        }
        for label, mutate in changes.items():
            with self.subTest(label):
                report = self.parse(mutate)
                self.assertEqual(report["outcome"], "validation_failed", report)


def chicago():
    with zipfile.ZipFile(FIXTURE / "chicago.zip") as archive:
        return {name.removesuffix(".json"): archive.read(name) for name in archive.namelist()}


class ChicagoTests(unittest.TestCase):
    """The Chicago Board of Elections' 11 race responses, captured November 23, 2026."""

    def test_the_fixture_is_the_captured_responses(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())["chicago"]
        for race, body in chicago().items():
            self.assertEqual(hashlib.sha256(body).hexdigest(), ledger[f"{race}.json"]["sha256"])

    def test_citywide_counts_and_their_wards_add_up(self):
        responses = chicago()
        self.assertEqual(len(responses), 11)
        senate = dict(il.chicago_results(responses["IL-SEN"], "IL-SEN"))
        self.assertEqual(
            senate,
            {
                "Don Tracy": 93434,
                "Juliana Stratton": 675842,
                "Whitfield Harrington Jr": 11527,
                "Chris Gant (Write-in)": 4,
            },
        )
        for race, body in responses.items():
            with self.subTest(race):
                self.assertTrue(il.chicago_results(body, race))

    def test_a_changed_count_is_refused(self):
        body = chicago()["IL-08"].decode()
        # The dialog's HTML is JSON-escaped: ">472<" is ">472<".
        for old, new in (("1,271", "1,272"), ("\\u003E472\\u003C", "\\u003E473\\u003C")):
            with self.subTest(old):
                self.assertIn(old, body)
                with self.assertRaises(ValueError):
                    il.chicago_results(body.replace(old, new, 1).encode(), "IL-08")


def platinum_henry():
    with zipfile.ZipFile(FIXTURE / "platinum_henry.zip") as archive:
        return [archive.read(f"races{n}.html") for n in (1, 2, 3)]


class PlatinumTests(unittest.TestCase):
    """Henry County's platinumelectionresults race pages, captured November 23, 2026."""

    def setUp(self):
        self.titles = il.table()["titles"]["Henry"]

    def test_the_fixture_is_the_captured_pages(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())["platinum_henry"]
        for n, body in enumerate(platinum_henry(), 1):
            self.assertEqual(hashlib.sha256(body).hexdigest(), ledger[f"races{n}.html"]["sha256"])

    def test_races_read_with_their_methods_adding_up(self):
        found = il.platinum_results(platinum_henry(), self.titles, "unofficial", "Henry")
        self.assertEqual(set(found), {"IL-SEN", "IL-GOV", "IL-16", "IL-17"})
        self.assertEqual(
            found["IL-SEN"],
            (
                [
                    ("Don Tracy", 11214),
                    ("Juliana Stratton", 7784),
                    ("Whitfield Harrington Jr", 333),
                ],
                (52, 52),
            ),
        )
        # A ticket's two names, printed on two lines, read as one name.
        self.assertEqual(found["IL-GOV"][0][1][0], "(JB Pritzker (Christian Mitchell")

    def test_a_changed_count_or_a_canvass_claim_is_refused(self):
        pages = platinum_henry()
        changed = [pages[0].replace(b"<td>9,185</td>", b"<td>9,186</td>", 1), *pages[1:]]
        with self.assertRaises(ValueError):
            il.platinum_results(changed, self.titles, "unofficial", "Henry")
        # The pages say "Unofficial Results": they cannot be the county's canvass.
        with self.assertRaises(ValueError):
            il.platinum_results(pages, self.titles, "canvassed", "Henry")


class LiveVoterTurnoutTests(unittest.TestCase):
    """Effingham County's livevoterturnout.com results page, captured November 23, 2026."""

    def setUp(self):
        self.page = (FIXTURE / "lvt_effingham.html").read_bytes()
        self.titles = il.table()["titles"]["Effingham"]

    def test_the_fixture_is_the_captured_page(self):
        ledger = json.loads((FIXTURE / "provenance.json").read_text())["lvt_effingham.html"]
        self.assertEqual(hashlib.sha256(self.page).hexdigest(), ledger["sha256"])

    def test_countywide_totals_add_up_by_method_and_by_precinct(self):
        found, when = il.livevoterturnout_results(self.page, self.titles, "Effingham")
        self.assertEqual(set(found), {"IL-SEN", "IL-GOV", "IL-12"})
        self.assertEqual(found["IL-12"][0], [("Julie Fortier", 3054), ("Mike Bost", 12338)])
        self.assertEqual(when.isoformat(), "2026-11-18T14:42:16-06:00")

    def test_a_changed_total_is_refused(self):
        changed = self.page.replace(b"1634, 31, 3113]", b"1634, 31, 3114]", 1)
        self.assertNotEqual(changed, self.page)
        with self.assertRaises(ValueError):
            il.livevoterturnout_results(changed, self.titles, "Effingham")


if __name__ == "__main__":
    unittest.main()
