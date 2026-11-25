"""The map builder: pinned vintages, every response kept by SHA-256, the registry's units."""

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fetch_boundaries", ROOT / "scripts/fetch_boundaries.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
spec = importlib.util.spec_from_file_location("compare_maps", ROOT / "scripts/compare_maps.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)

PAYLOAD = json.dumps({"features": [{"attributes": {"STATE": "50"}, "geometry": {"rings": []}}]})


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MapSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_dated_vintage_is_pinned_and_places_come_from_the_registry(self):
        self.assertRegex(builder.VINTAGE, r"^tigerWMS_ACS\d{4}$")
        self.assertRegex(builder.MISSOURI_VINTAGE, r"^tigerWMS_ACS\d{4}$")
        self.assertEqual(builder.TOWN_STATES, {"VT", "CT", "MA"})
        self.assertEqual(builder.HOUSE_DISTRICT_STATES, {"AK"})

    def test_requests_go_out_under_the_pipelines_own_user_agent(self):
        settings, _ = load_config(ROOT / "config")
        self.assertEqual(builder.USER_AGENT, settings.user_agent)
        seen = []

        def urlopen(request, timeout):
            seen.append(request.get_header("User-agent"))
            return Response(PAYLOAD.encode())

        with patch.object(builder.urllib.request, "urlopen", urlopen):
            builder.Archive(self.root).fetch("https://example.test/q")
        self.assertEqual(seen, [settings.user_agent])

    def test_each_response_is_kept_by_sha256_and_rebuilds_offline(self):
        url = "https://example.test/q?where=1"
        with patch.object(
            builder.urllib.request, "urlopen", lambda request, timeout: Response(PAYLOAD.encode())
        ):
            online = builder.Archive(self.root).fetch(url)
        digest = hashlib.sha256(online).hexdigest()
        self.assertEqual((self.root / f"{digest}.json").read_bytes(), online)
        with patch.object(builder.urllib.request, "urlopen", side_effect=AssertionError("request")):
            offline = builder.Archive(self.root, offline=True)
            self.assertEqual(offline.fetch(url), online)
            with self.assertRaisesRegex(ValueError, "Not in the archive"):
                offline.fetch("https://example.test/other")
        (self.root / f"{digest}.json").write_bytes(online + b" ")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            builder.Archive(self.root, offline=True).fetch(url)

    def test_a_query_names_the_dated_service_and_records_its_hash(self):
        urls = []

        def fetch(self, url):
            urls.append(url)
            return PAYLOAD.encode()

        with patch.object(builder.Archive, "fetch", fetch):
            _, source = builder.query(builder.VINTAGE, builder.COUNTY_LAYER, "1=1", "STATE")
        self.assertIn(f"/{builder.VINTAGE}/MapServer/{builder.COUNTY_LAYER}/query?", urls[0])
        self.assertEqual(source["sha256"], hashlib.sha256(PAYLOAD.encode()).hexdigest())

    def test_the_comparison_reports_added_removed_renamed_and_moved_units(self):
        def unit(name, x, ident=None):
            return {
                "id": ident or name.upper(),
                "name": name,
                "path": f"M{x},10L{x + 5},10L{x + 5},20Z",
                "center": [x + 2, 14],
            }

        old = [unit("Adams", 10), unit("Bay", 40), unit("Clay", 70)]
        new = [unit("Adams", 10), unit("Bay Town", 43, "BAY"), unit("Dale", 90)]
        found = comparison.compare_units(old, new)
        self.assertEqual((found["old"], found["new"]), (3, 3))
        self.assertEqual(found["added"], ["DALE"])
        self.assertEqual(found["removed"], ["CLAY"])
        self.assertEqual(found["renamed"], [("BAY", "Bay", "Bay Town")])
        self.assertEqual(found["reshaped"], 1)
        self.assertEqual(found["max_shift"], 3.0)


if __name__ == "__main__":
    unittest.main()
