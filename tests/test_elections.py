"""The 2026 election definition reproduces today's registry, sources and acquisition exactly."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from election_pipeline import elections
from election_pipeline.elections import Acquisition, describe, durable, fingerprint, load
from election_pipeline.settings import load_config
from election_pipeline.state_model import State
from election_pipeline.states import STATES

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests/fixtures/registry/registry_2026.json"
MANUAL = {
    "az_2026_general",
    "il_2026_general",
    "ks_2026_general",
    "ky_2026_general",
    "me_2026_general",
    "mi_2026_general",
    "mo_2026_general",
    "nh_2026_general",
    "nj_2026_general",
    "nv_2026_general",
    "ny_2026_general",
    "oh_2026_general",
    "ok_2026_general",
    "pa_2026_general",
    "tn_2026_general",
    "wi_2026_general",
    "wy_2026_general",
}


class DefinitionTests(unittest.TestCase):
    def test_the_definition_reproduces_the_golden_registry_exactly(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        found = describe(load())
        # Compared part by part so a difference names the state or source it is in.
        self.assertEqual(found["collector"], golden["collector"])
        for part in ("states", "sources", "acquisition"):
            self.assertEqual(set(found[part]), set(golden[part]), part)
            for name in golden[part]:
                self.assertEqual(found[part][name], golden[part][name], f"{part} {name}")
        self.assertEqual(found, golden)

    def test_the_registry_and_the_sources_are_the_definition(self):
        election = load()
        settings, sources = load_config(ROOT / "config")
        self.assertEqual(STATES, election.states)
        self.assertEqual(sources, election.sources)
        self.assertEqual(settings, election.settings)
        # The golden record, through the code that reads the definition.
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(describe(election)["states"], golden["states"])
        # An election's own folder and the config folder name the same election.
        self.assertEqual(load_config(ROOT / "config/elections/2026-general")[1], sources)

    def test_each_state_has_one_file_and_each_source_one_acquisition(self):
        election = load()
        self.assertEqual(set(election.states), set(durable()))
        self.assertEqual(set(election.acquisition), set(election.sources))
        self.assertEqual({s.election_date for s in election.sources.values()}, {election.date})
        for code, state in election.states.items():
            self.assertEqual(election.sources[state.source_id].state, code)

    def test_manual_sources_are_declared_with_their_freshness_and_final_flag(self):
        election = load()
        manual = {n for n, a in election.acquisition.items() if a.mode == "manual"}
        self.assertEqual(manual, MANUAL)
        # Sixteen allow six hours between imports; Oklahoma, certified, allows two days.
        limits = {n: election.acquisition[n].max_age_seconds for n in manual}
        self.assertEqual({v for n, v in limits.items() if n != "ok_2026_general"}, {21600})
        self.assertEqual(limits["ok_2026_general"], 172800)
        # Final: the issuer posts nothing more (Kansas, Maine, Michigan) or the state has
        # certified (Ohio, Oklahoma, Wyoming).
        self.assertEqual(
            {n for n in manual if election.acquisition[n].final},
            {
                "ks_2026_general",
                "me_2026_general",
                "mi_2026_general",
                "oh_2026_general",
                "ok_2026_general",
                "wy_2026_general",
            },
        )
        self.assertEqual(
            {a.mode for n, a in election.acquisition.items() if n not in manual}, {"http"}
        )

    def test_an_acquisition_must_be_known_and_http_has_no_freshness_limit(self):
        for options in (
            {"mode": "carrier-pigeon"},
            {"mode": "http", "final": True},
            {"mode": "http", "max_age_seconds": 60},
        ):
            with self.subTest(options), self.assertRaises(ValueError):
                Acquisition(**options)


class FingerprintTests(unittest.TestCase):
    def test_a_states_fingerprint_follows_its_file_and_its_durable_facts_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            shutil.copytree(elections.CONFIG, config)
            before = {code: fingerprint(code, config=config) for code in ("CT", "KY", "NC")}
            self.assertEqual(len(set(before.values())), 3)
            path = config / "elections/2026-general/states/KY.toml"
            path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
            after = {code: fingerprint(code, config=config) for code in before}
            self.assertEqual({c for c in before if before[c] != after[c]}, {"KY"})
            # A durable fact of one state changes that state's fingerprint only.
            facts = config / "states.toml"
            facts.write_text(
                facts.read_text(encoding="utf-8").replace('"Kentucky"', '"Kentucky!"'),
                encoding="utf-8",
            )
            again = {code: fingerprint(code, config=config) for code in before}
            self.assertEqual({c for c in before if after[c] != again[c]}, {"KY"})
            # The election's own file (collector limits, TLS exceptions) applies to every state.
            head = config / "elections/2026-general/election.toml"
            head.write_text(head.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
            every = {code: fingerprint(code, config=config) for code in before}
            self.assertTrue(all(every[c] != again[c] for c in before))
            # Line endings (a Windows checkout) do not change it.
            unix = path.read_bytes().replace(b"\r\n", b"\n")
            path.write_bytes(unix.replace(b"\n", b"\r\n"))
            self.assertEqual(fingerprint("KY", config=config), every["KY"])

    def test_durable_facts_are_checked_as_they_load(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            (config / "states.toml").write_text('[CT]\nname = "Connecticut"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "a name, a local unit"):
                durable(config)
            (config / "states.toml").write_text(
                '[CT]\nname = "x"\nlocal_unit = "town"\nflag = true\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "nothing but"):
                durable(config)


class StateModelTests(unittest.TestCase):
    """What a registry entry must satisfy (R-010) and which flags may go together (R-003)."""

    def make(self, **changes):
        fields = {
            "code": "ZZ",
            "name": "Zed",
            "source_id": "zz_2026_general",
            "house": (1, 2),
            "methods": ("total_votes", "early"),
        }
        return State(**{**fields, **changes})

    def test_a_valid_entry_is_built_from_keywords_only(self):
        self.assertEqual(sorted(self.make().races), ["ZZ-01", "ZZ-02", "ZZ-SEN"])
        with self.assertRaises(TypeError):
            State("ZZ", "Zed", "zz_2026_general", (1,), ("total_votes",))

    def test_invariants_are_enforced_where_an_entry_is_built(self):
        for changes, message in (
            ({"code": "Zz"}, "two capital letters"),
            ({"name": ""}, "needs a name"),
            ({"source_id": "yy_2026_general"}, "must start with zz_"),
            ({"methods": ()}, "first vote method"),
            ({"methods": ("early", "total_votes")}, "first vote method"),
            ({"methods": ("total_votes", "early", "early")}, "listed twice"),
            ({"house": (2, 1)}, "sorted, unique"),
            ({"house": (1, 1)}, "sorted, unique"),
            ({"house": (), "senate": False}, "elects no"),
            ({"local_unit": "ward"}, "local unit"),
        ):
            with self.subTest(changes), self.assertRaisesRegex(ValueError, message):
                self.make(**changes)

    def test_flag_combinations_that_cannot_both_hold_are_refused(self):
        with self.assertRaisesRegex(ValueError, "not both"):
            self.make(optional_house=True, collected_counties=True)
        with self.assertRaisesRegex(ValueError, "no House races"):
            self.make(house=(), optional_house=True)
        with self.assertRaisesRegex(ValueError, "county_files"):
            self.make(county_feed_status=True)
        entry = self.make(county_feed_status=True, collection_backend="county_files")
        self.assertTrue(entry.county_feed_status)

    def test_every_registered_state_meets_them(self):
        # Built from the definition, so each passed these checks to exist.
        self.assertEqual(len(STATES), 50)
        for state in STATES.values():
            self.assertEqual(state.methods[0], "total_votes")


class RefusalTests(unittest.TestCase):
    """A definition is checked as it loads: a typo is an error, not a silent default."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(elections.ROOT / "2026-general", self.root / "2026-general")
        self.folder = self.root / "2026-general"

    def edit(self, relative, old, new):
        path = self.folder / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")

    def refused(self, message):
        with self.assertRaisesRegex(ValueError, message):
            load("2026-general", self.root)

    def test_the_copy_loads(self):
        self.assertEqual(describe(load("2026-general", self.root)), describe(load()))

    def test_an_election_names_itself(self):
        self.edit("election.toml", 'id = "2026-general"', 'id = "2028-general"')
        self.refused("says it is")

    def test_a_state_file_belongs_to_its_own_state(self):
        self.edit("states/CT.toml", 'code = "CT"', 'code = "VT"')
        self.refused("must be a known state's, CT")

    def test_durable_facts_cannot_be_redefined_per_election(self):
        self.edit(
            "states/CT.toml",
            'methods = ["total_votes"]',
            'methods = ["total_votes"]\nname = "Elsewhere"',
        )
        self.refused("unknown state settings")

    def test_unknown_keys_are_errors(self):
        self.edit("states/CT.toml", "[races]", "[races]\nlieutenant_governor = true")
        self.refused("races hold senate")
        self.edit("states/CT.toml", "lieutenant_governor = true\n", "")
        self.edit("states/CT.toml", "[source]", "[source]\nverify_tls = false")
        self.refused("unknown source settings")

    def test_a_source_needs_an_id_and_its_state_one_acquisition_mode_from_a_known_set(self):
        self.edit("states/CT.toml", 'id = "ct_2026_general"\n', "")
        self.refused("needs an id")
        self.edit("states/CT.toml", "[source]", '[source]\nid = "ct_2026_general"')
        self.edit("states/CT.toml", 'mode = "http"', 'mode = "ftp"')
        self.refused("Acquisition must be one of")


if __name__ == "__main__":
    unittest.main()
