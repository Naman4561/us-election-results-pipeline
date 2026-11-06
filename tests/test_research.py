"""Version integrity and behavior of the local research sandbox."""

import hashlib
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from election_pipeline.research.__main__ import import_parsed
from election_pipeline.research.catalog import Catalog

try:
    from fastapi.testclient import TestClient

    from election_pipeline.research.query import Query, explore
    from election_pipeline.research.server import create_app
except ImportError:
    TestClient = None


class ResearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.catalog = Catalog(self.root / "store")
        self.source = self.root / "population.csv"
        self.source.write_text(
            "county_fips,county,population\n01001,Autauga,15\n01003,Baldwin,200\n"
            "01005,Barbour,\n01007,Baldwin,45\n",
            encoding="utf-8",
        )
        self.metadata = {
            "dataset_id": "us.county_population",
            "title": "County populations",
            "kind": "census",
            "status": "published",
            "jurisdiction": "US",
            "geography": "county",
            "grain": "One row per county",
            "period_start": "2024-01-01",
            "publisher": "Test publisher",
            "source_url": "https://example.com/population",
            "tags": ["Population", "historical"],
        }

    def test_versions_preserve_originals_and_reject_an_invalid_type_atomically(self):
        first = self.catalog.import_file(self.source, self.metadata)
        self.assertEqual(first["row_count"], 4)
        self.assertEqual(
            self.catalog.import_file(self.source, self.metadata)["outcome"], "already_imported"
        )
        metadata = {**self.metadata, "status": "provisional"}
        second = self.catalog.import_file(self.source, metadata)
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(self.catalog.version(first["version_id"])["status"], "published")
        with self.catalog.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)

        with self.assertRaisesRegex(ValueError, "Invalid integer"):
            self.catalog.import_file(
                self.source, {**metadata, "column_types": {"county": "integer"}}
            )
        with self.assertRaisesRegex(ValueError, "different data scope"):
            self.catalog.import_file(self.source, {**metadata, "geography": "state"})
        self.assertEqual(len(self.catalog.versions()), 2)
        file_id, _, _ = self.catalog.file(second["version_id"], 0)
        self.assertEqual(b"".join(self.catalog.file_chunks(file_id)), self.source.read_bytes())

    def test_parser_snapshot_import_verifies_csv_and_attached_raw_hashes(self):
        source = self.root / "county_results.csv"
        source.write_bytes(self.source.read_bytes())
        original = self.root / "original.zip"
        original.write_bytes(b"original bytes")
        report = {
            "outcome": "parsed",
            "dataset": "election_results",
            "source_id": "nc_2026_general",
            "election_date": "2026-11-03",
            "source_url": "https://example.com/results",
            "output_sha256": {source.name: hashlib.sha256(source.read_bytes()).hexdigest()},
            "identity": {"raw_sha256": hashlib.sha256(original.read_bytes()).hexdigest()},
        }
        (self.root / "report.json").write_text(json.dumps(report), encoding="utf-8")
        arguments = Namespace(
            directory=self.root,
            table=source.name,
            jurisdiction="NC",
            publisher="Test publisher",
            status="unofficial",
            as_of=None,
            original=original,
        )
        result = import_parsed(self.catalog, arguments)
        version = self.catalog.version(result["version_id"])
        self.assertEqual(version["dataset_id"], "nc_2026_general.county_results")
        self.assertEqual(version["parent_sha256"], report["identity"]["raw_sha256"])
        self.assertEqual(
            [file["filename"] for file in version["files"]],
            ["county_results.csv", "report.json", "original.zip"],
        )
        original.write_bytes(b"changed original")
        with self.assertRaisesRegex(ValueError, "original.zip SHA-256"):
            import_parsed(self.catalog, arguments)
        source.write_bytes(b"changed csv")
        with self.assertRaisesRegex(ValueError, "county_results.csv SHA-256"):
            import_parsed(self.catalog, arguments)
        self.assertEqual(len(self.catalog.versions()), 1)

    @unittest.skipUnless(TestClient, "Install the dashboard and test dependencies")
    def test_filters_pagination_and_statistics_preserve_text_ids_and_nulls(self):
        version_id = self.catalog.import_file(self.source, self.metadata)["version_id"]
        result = explore(
            self.catalog,
            version_id,
            Query(
                filters=[{"column": "population", "operator": "gte", "value": "15"}],
                sort="population",
                direction="desc",
                statistic="population",
                limit=1,
                offset=1,
            ),
        )
        self.assertEqual(result["rows"], [["01007", "Baldwin", 45]])
        self.assertEqual(result["matching_rows"], 3)
        self.assertEqual(result["statistics"]["sum"], 260)
        self.assertEqual(result["statistics"]["min"], 15)
        self.assertEqual(result["statistics"]["max"], 200)
        result = explore(
            self.catalog,
            version_id,
            Query(
                sort="population",
                direction="desc",
                statistic="county",
            ),
        )
        self.assertEqual(result["rows"][-1], ["01005", "Barbour", None])
        self.assertEqual(result["statistics"]["most_common"][0], {"value": "Baldwin", "count": 2})
        result = explore(
            self.catalog,
            version_id,
            Query(
                filters=[{"column": "county", "operator": "eq", "value": "' OR 1=1 --"}],
                statistic="population",
            ),
        )
        self.assertEqual(result["matching_rows"], 0)
        self.assertIsNone(result["statistics"]["sum"])

    @unittest.skipUnless(TestClient, "Install the dashboard and test dependencies")
    def test_api_local_host_boundaries_and_archive_download(self):
        original = self.root / "original.pdf"
        original.write_bytes(b"%PDF fixture" * 40000)
        version_id = self.catalog.import_file(original, self.metadata, archive_only=True)[
            "version_id"
        ]
        with TestClient(create_app(self.catalog.root), base_url="http://127.0.0.1") as client:
            self.assertIn("Election Data Sandbox", client.get("/").text)
            self.assertEqual(
                client.get("/api/catalog", headers={"Host": "example.net"}).status_code, 400
            )
            self.assertIsNone(client.get(f"/api/versions/{version_id}").json()["row_count"])
            response = client.get(f"/api/versions/{version_id}/files/0")
            self.assertEqual(response.content, original.read_bytes())
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(
                client.post(f"/api/versions/{version_id}/query", json={}).status_code, 400
            )
            self.assertEqual(client.post("/api/versions/999/query", json={}).status_code, 404)
            self.assertEqual(
                client.post("/api/versions/999/query", json={"limit": 101}).status_code, 422
            )
