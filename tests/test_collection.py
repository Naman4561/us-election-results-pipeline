"""Offline behavioral tests against a real loopback HTTP server; fixtures are synthetic."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from election_pipeline import pipeline
from election_pipeline.checks import InvalidArchive, validate_archive
from election_pipeline.cli import main
from election_pipeline.pipeline import collect_source, retry_after_seconds
from election_pipeline.settings import CollectorSettings, Source, load_config
from election_pipeline.storage import ArchiveStore


def synthetic_zip(payload: bytes = b"synthetic\n10\n", name: str = "results.txt") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(name, payload)
    return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    responses: list[dict] = []
    requests: list[dict] = []

    def do_GET(self) -> None:
        self.requests.append({"path": self.path, "headers": dict(self.headers.items())})
        response = self.responses.pop(0) if self.responses else {"status": 500}
        self.send_response(response.get("status", 200))
        body = response.get("body", b"")
        headers = response.get("headers", {})
        if "Content-Length" not in headers and not response.get("omit_length"):
            self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def log_message(self, *args) -> None:
        pass


class CollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.url = f"http://127.0.0.1:{self.server.server_port}/results.zip"
        self.source = Source(
            "test_source", "NC", "2026-11-03", self.url, expected_members=("results.txt",)
        )
        self.settings = CollectorSettings(backoff_seconds=0, max_retry_wait_seconds=0)
        self.body = synthetic_zip()
        Handler.responses = []
        Handler.requests = []
        # Most tests exercise one run after another; the persistent backoff between failed
        # runs has its own tests below.
        immediate = patch("election_pipeline.pipeline.BACKOFF_BASE_SECONDS", 0)
        immediate.start()
        self.addCleanup(immediate.stop)

    def collect(self, **kwargs) -> dict:
        return collect_source(self.source, self.settings, self.root, **kwargs)

    def good(self, body: bytes | None = None, **headers) -> dict:
        return {"body": self.body if body is None else body, "headers": headers}

    def records(self) -> list[dict]:
        return [json.loads(path.read_text()) for path in self.root.glob("metadata/**/*.json")]

    def state(self) -> dict:
        """Cache state; failure bookkeeping (and the identity it is stored under) is
        checked separately, so {} still means no cache or validator was saved."""
        state = ArchiveStore(self.root, self.source).read_state()
        state = {k: v for k, v in state.items() if k not in pipeline.FAILURE_KEYS}
        return {} if state.keys() <= {"schema_version", "source_signature", "url"} else state

    def raw_state(self) -> dict:
        return ArchiveStore(self.root, self.source).read_state()

    def test_failed_runs_back_off_without_requests_until_due(self) -> None:
        with patch("election_pipeline.pipeline.BACKOFF_BASE_SECONDS", 300):
            Handler.responses = [{"status": 404}]
            self.assertEqual(self.collect()["outcome"], "http_error")
            state = self.raw_state()
            self.assertEqual(state["consecutive_failures"], 1)
            self.assertIsNone(state["blocked_since"])
            waiting = self.collect()
            self.assertEqual(waiting["outcome"], "backing_off")
            self.assertEqual(len(Handler.requests), 1)  # no request while backing off
            self.assertEqual(len(self.records()), 1)  # and nothing recorded per tick
            # An operator's forced download still goes out, and success clears the count.
            Handler.responses = [self.good()]
            self.assertEqual(self.collect(force_download=True)["outcome"], "downloaded")
            self.assertFalse(set(pipeline.FAILURE_KEYS) & self.raw_state().keys())

    @patch("election_pipeline.pipeline.BACKOFF_BASE_SECONDS", 300)
    def test_backoff_grows_and_access_refusals_wait_longer(self) -> None:
        self.assertEqual(
            [pipeline.failure_backoff(n, False) for n in (1, 2, 3, 4, 9)],
            [300, 900, 2700, 3600, 3600],
        )
        self.assertEqual(pipeline.failure_backoff(9, True), 6 * 3600)
        for _ in range(2):
            Handler.responses = [{"status": 403}]
            self.assertEqual(self.collect(force_download=True)["outcome"], "http_error")
        state = self.raw_state()
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertIsNotNone(state["blocked_since"])
        self.assertEqual(state["last_failure"], "HTTP 403")

    def archives(self) -> list[Path]:
        return list(self.root.glob("raw/**/*.zip"))

    def test_first_download_is_original_bytes_with_provenance(self) -> None:
        Handler.responses = [self.good(ETag='"v1"')]
        result = self.collect()
        self.assertEqual(result["outcome"], "downloaded")
        self.assertTrue(result["archive_created"])
        self.assertEqual(self.archives()[0].read_bytes(), self.body)
        self.assertEqual(result["sha256"], hashlib.sha256(self.body).hexdigest())
        self.assertEqual(self.state()["etag"], '"v1"')
        record = self.records()[0]
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["verification_level"], "archive_only")
        self.assertEqual(record["zip_members"][0]["name"], "results.txt")

    def test_restart_and_304_reuse_archive_and_persist_conditions(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), {"status": 304}]
        first = self.collect()
        second = self.collect()  # A fresh collector/store reads persistent state.
        self.assertEqual(second["outcome"], "unchanged")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(Handler.requests[1]["headers"]["If-None-Match"], '"v1"')
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_repeated_200_deduplicates_identical_bytes(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), self.good(ETag='"v2"')]
        first = self.collect()
        second = self.collect()
        self.assertEqual(second["outcome"], "unchanged")
        self.assertFalse(second["archive_created"])
        self.assertEqual(self.state()["etag"], '"v2"')
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_last_modified_fallback(self) -> None:
        modified = "Mon, 05 Oct 2026 14:57:45 GMT"
        Handler.responses = [self.good(**{"Last-Modified": modified}), {"status": 304}]
        self.collect()
        self.collect()
        self.assertEqual(Handler.requests[1]["headers"]["If-Modified-Since"], modified)

    def test_invalid_zip_is_quarantined_and_cannot_poison_validators(self) -> None:
        Handler.responses = [
            self.good(ETag='"valid"'),
            self.good(b"<html>broken</html>", ETag='"bad"'),
            {"status": 304},
        ]
        first = self.collect()
        invalid = self.collect()
        self.assertEqual(invalid["outcome"], "invalid_archive")
        self.assertEqual(self.state()["sha256"], first["sha256"])
        self.assertEqual(self.state()["etag"], '"valid"')
        self.assertEqual(len(list(self.root.glob("quarantine/**/*.bin"))), 1)
        self.assertEqual(self.collect()["outcome"], "unchanged")
        self.assertEqual(Handler.requests[2]["headers"]["If-None-Match"], '"valid"')

    def test_wrong_member_cannot_advance_state(self) -> None:
        Handler.responses = [self.good(synthetic_zip(name="other-election.txt"))]
        self.assertEqual(self.collect()["outcome"], "invalid_archive")
        self.assertEqual(self.state(), {})
        self.assertEqual(len(self.archives()), 0)

    def test_bad_crc_is_rejected(self) -> None:
        payload = b"unique-synthetic-payload"
        body = bytearray(synthetic_zip(payload))
        body[body.index(payload)] ^= 1
        Handler.responses = [self.good(bytes(body))]
        self.assertEqual(self.collect()["outcome"], "invalid_archive")
        self.assertEqual(self.state(), {})

    def test_corrupt_deflate_stream_is_quarantined(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("results.txt", b"synthetic data" * 100)
        body = bytearray(output.getvalue())
        member_data_start = 30 + len("results.txt")
        body[member_data_start : member_data_start + 4] = b"\xff\xff\xff\xff"
        Handler.responses = [self.good(bytes(body))]
        self.assertEqual(self.collect()["outcome"], "invalid_archive")
        self.assertEqual(len(list(self.root.glob("quarantine/**/*.bin"))), 1)

    def test_truncated_response_retries_and_logs_each_attempt(self) -> None:
        Handler.responses = [
            self.good(**{"Content-Length": str(len(self.body) + 20)}),
            self.good(),
        ]
        self.assertEqual(self.collect()["outcome"], "downloaded")
        records = sorted(self.records(), key=lambda value: value["attempt"])
        self.assertEqual([record["outcome"] for record in records], ["network_error", "downloaded"])
        self.assertEqual(records[0]["http_status"], 200)

    def test_503_retry_and_distinct_fetch_ids(self) -> None:
        Handler.responses = [{"status": 503, "headers": {"Retry-After": "0"}}, self.good()]
        result = self.collect()
        self.assertEqual(result["outcome"], "downloaded")
        self.assertEqual(result["attempt"], 2)
        records = self.records()
        self.assertEqual(len({record["fetch_id"] for record in records}), 2)
        self.assertEqual(len({record["run_id"] for record in records}), 1)

    def test_redirects_are_checked_before_they_are_followed(self) -> None:
        # A plain-HTTP target off loopback is refused before any request is made to it.
        Handler.responses = [{"status": 302, "headers": {"Location": "http://example.com/x.zip"}}]
        result = self.collect()
        self.assertEqual(result["outcome"], "network_error")
        self.assertIn("Refused redirect", result["error"])
        self.assertEqual(len(Handler.requests), 1)
        # An allowed target is followed as before.
        Handler.responses = [
            {"status": 302, "headers": {"Location": self.url + "?moved"}},
            self.good(),
        ]
        self.assertEqual(self.collect(force_download=True)["outcome"], "downloaded")
        self.assertEqual(Handler.requests[-1]["path"], "/results.zip?moved")

    def test_terminal_404_is_not_retried(self) -> None:
        Handler.responses = [{"status": 404}]
        self.assertEqual(self.collect()["outcome"], "http_error")
        self.assertEqual(len(Handler.requests), 1)
        self.assertEqual(self.state(), {})

    def test_retries_stop_at_configured_limit(self) -> None:
        Handler.responses = [{"status": 500}] * 3
        self.assertEqual(self.collect()["outcome"], "http_error")
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(len(Handler.requests), 3)

    def test_network_exception_retries_without_advancing_state(self) -> None:
        with patch("election_pipeline.collect.open_url", side_effect=URLError("simulated timeout")):
            self.assertEqual(self.collect()["outcome"], "network_error")
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(self.state(), {})
        self.assertEqual(len(self.archives()), 0)

    def test_long_retry_after_persists_across_runs_and_force(self) -> None:
        Handler.responses = [{"status": 429, "headers": {"Retry-After": "3600"}}]
        self.assertEqual(self.collect()["outcome"], "deferred")
        self.assertIn("retry_not_before", self.state())
        self.assertEqual(self.collect(force_download=True)["outcome"], "deferred")
        self.assertEqual(len(Handler.requests), 1)
        self.assertEqual(len(self.records()), 2)

    def test_unrepresentably_large_retry_after_defers_without_crashing(self) -> None:
        Handler.responses = [{"status": 429, "headers": {"Retry-After": "9" * 100}}]
        self.assertEqual(self.collect()["outcome"], "deferred")
        self.assertTrue(self.state()["retry_not_before"].startswith("9999-"))

    def test_force_download_skips_conditions_but_still_deduplicates(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), self.good()]
        self.collect()
        self.assertEqual(self.collect(force_download=True)["outcome"], "unchanged")
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])
        self.assertEqual(len(self.archives()), 1)

    def test_changed_url_does_not_reuse_old_validator(self) -> None:
        Handler.responses = [self.good(ETag='"old-url"'), self.good()]
        self.collect()
        self.source = replace(self.source, url=self.url + "?version=other")
        self.collect()
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])

    def test_new_signature_keeps_the_snapshot_and_failures_but_not_validators(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), {"status": 503}]
        first = self.collect()
        # A release that signs the source differently, while the site is down.
        self.settings = replace(self.settings, max_csv_rows=self.settings.max_csv_rows - 1)
        self.assertEqual(self.collect(force_download=True)["outcome"], "http_error")
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])
        state = self.raw_state()
        self.assertEqual(state["source_signature"], self.source.signature(self.settings))
        self.assertEqual(state["sha256"], first["sha256"])
        self.assertIn("last_checked_at", state)
        self.assertEqual(state["consecutive_failures"], 1)
        self.assertNotIn("etag", state)
        self.assertIsNotNone(ArchiveStore(self.root, self.source).cached_archive(state))

    def test_missing_cached_file_forces_a_full_download(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), self.good(ETag='"v1"')]
        self.collect()
        self.archives()[0].unlink()
        self.assertEqual(self.collect()["outcome"], "unchanged")
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])
        self.assertEqual(self.archives()[0].read_bytes(), self.body)

    def test_corrupt_historical_archive_is_not_silently_overwritten(self) -> None:
        Handler.responses = [self.good(ETag='"v1"'), self.good()]
        self.collect()
        path = self.archives()[0]
        path.write_bytes(b"damaged-on-disk")
        result = self.collect()
        self.assertEqual(result["outcome"], "storage_or_validation_error")
        self.assertEqual(path.read_bytes(), b"damaged-on-disk")
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])

    def test_source_lock_prevents_overlapping_http_requests(self) -> None:
        with ArchiveStore(self.root, self.source).lock():
            self.assertEqual(self.collect()["outcome"], "locked")
        self.assertEqual(len(Handler.requests), 0)

    def test_download_limit_with_and_without_content_length(self) -> None:
        self.settings = replace(self.settings, max_download_bytes=10)
        Handler.responses = [self.good(), {**self.good(), "omit_length": True}]
        for _ in range(2):
            self.assertEqual(self.collect()["outcome"], "network_error")
        self.assertEqual(len(Handler.requests), 2)
        self.assertEqual(self.state(), {})

    def test_uncompressed_limit(self) -> None:
        self.settings = replace(self.settings, max_uncompressed_bytes=2)
        Handler.responses = [self.good()]
        self.assertEqual(self.collect()["outcome"], "invalid_archive")

    def test_reappearance_reuses_bytes_but_is_a_changed_observation(self) -> None:
        other = synthetic_zip(b"synthetic\n8\n")
        Handler.responses = [self.good(), self.good(other), self.good()]
        self.collect()
        self.collect()
        result = self.collect()
        self.assertTrue(result["changed"])
        self.assertFalse(result["archive_created"])
        self.assertEqual(len(self.archives()), 2)
        self.assertEqual(self.state()["sha256"], hashlib.sha256(self.body).hexdigest())

    def test_regressed_source_timestamp_is_flagged_without_discarding_bytes(self) -> None:
        Handler.responses = [
            self.good(**{"Last-Modified": "Tue, 06 Oct 2026 14:57:45 GMT"}),
            self.good(
                synthetic_zip(b"changed"), **{"Last-Modified": "Mon, 05 Oct 2026 14:57:45 GMT"}
            ),
        ]
        self.collect()
        self.collect()
        self.assertTrue(any(record.get("source_time_regressed") for record in self.records()))
        self.assertEqual(len(self.archives()), 2)

    def test_unsolicited_304_without_cache_retries_unconditionally(self) -> None:
        Handler.responses = [{"status": 304}, self.good()]
        self.assertEqual(self.collect()["outcome"], "downloaded")
        self.assertNotIn("If-None-Match", Handler.requests[1]["headers"])

    def test_failed_cache_write_does_not_lose_archive_and_next_run_recovers(self) -> None:
        Handler.responses = [self.good(), self.good()]
        with patch.object(ArchiveStore, "write_state", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(self.state(), {})
        self.assertEqual(len(self.archives()), 1)
        recovered = self.collect()
        self.assertEqual(recovered["outcome"], "downloaded")
        self.assertFalse(recovered["archive_created"])

    def test_failed_metadata_completion_does_not_advance_cache(self) -> None:
        Handler.responses = [self.good()]
        original = ArchiveStore.write_attempt

        def fail_completion(store, record):
            if record["outcome"] == "downloaded":
                raise OSError("simulated metadata write failure")
            return original(store, record)

        with patch.object(ArchiveStore, "write_attempt", new=fail_completion):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(self.state(), {})
        self.assertEqual(self.records()[0]["outcome"], "in_progress")

    def test_abandoned_partial_is_removed_under_lock(self) -> None:
        store = ArchiveStore(self.root, self.source)
        partial = store.temporary_download()
        partial.write_bytes(b"interrupted")
        Handler.responses = [self.good()]
        self.collect()
        self.assertFalse(partial.exists())

    def test_second_zip_source_uses_same_pipeline(self) -> None:
        self.source = replace(self.source, source_id="another_state", state="MN")
        Handler.responses = [self.good()]
        self.assertEqual(self.collect()["outcome"], "downloaded")
        self.assertEqual(len(list(self.root.glob("raw/MN/**/*.zip"))), 1)

    def test_cli_all_continues_after_one_source_fails(self) -> None:
        config = self.root / "sources.toml"
        config.write_text(
            "[collector]\nbackoff_seconds=0\n"
            f'[sources.first]\nstate="NC"\nelection_date="2026-11-03"\nurl="{self.url}"\n'
            f'[sources.second]\nstate="MN"\nelection_date="2026-11-03"\nurl="{self.url}?other"\n',
            encoding="utf-8",
        )
        Handler.responses = [{"status": 404}, self.good()]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = main(
                ["collect", "--all", "--config", str(config), "--data-dir", str(self.root)]
            )
        self.assertEqual(result, 1)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([record["outcome"] for record in records], ["http_error", "downloaded"])
        self.assertEqual(len(Handler.requests), 2)


class ConfigurationAndCheckTests(unittest.TestCase):
    def test_real_configuration_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        settings, sources = load_config(root / "config" / "sources.toml")
        self.assertEqual(
            sources["nc_2026_general_all"].expected_members, ("results_pct_20261103.txt",)
        )
        self.assertEqual(settings.max_attempts, 3)

    def test_config_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text("[collector]\ntimeout_typo = 10\n", encoding="utf-8")
            with self.assertRaises(TypeError):
                load_config(path)

    def test_config_rejects_string_instead_of_member_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                '[sources.nc]\nstate="NC"\nelection_date="2026-11-03"\n'
                'url="https://example.org/results.zip"\nexpected_members="results.txt"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(path)

    def test_source_rejects_path_traversal_and_remote_plain_http(self) -> None:
        for source_id, url in [
            ("../bad", "https://example.org/a"),
            ("okay", "http://example.org/a"),
        ]:
            with self.assertRaises(ValueError):
                Source(source_id, "NC", "2026-11-03", url)

    def test_zip_traversal_and_duplicate_members_are_rejected(self) -> None:
        source = Source("test", "NC", "2026-11-03", "https://example.org/a")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.zip"
            path.write_bytes(synthetic_zip(name="../escape.txt"))
            with self.assertRaises(InvalidArchive):
                validate_archive(path, source, CollectorSettings())
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("duplicate.txt", b"first")
                with self.assertWarns(UserWarning):
                    archive.writestr("duplicate.txt", b"second")
            with self.assertRaises(InvalidArchive):
                validate_archive(path, source, CollectorSettings())

    def test_retry_after_seconds_and_http_date(self) -> None:
        self.assertEqual(retry_after_seconds("120"), 120)
        self.assertIsNone(retry_after_seconds("not-a-date"))
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        seconds = retry_after_seconds(future.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.assertGreater(seconds, 118)
        self.assertLessEqual(seconds, 120)

    def test_cli_list_and_unknown_source(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = str(root / "config" / "sources.toml")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["list-sources", "--config", config]), 0)
            listed = [json.loads(line)["source_id"] for line in output.getvalue().splitlines()]
            self.assertIn("nc_2026_general_all", listed)
        with patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(main(["collect", "--source", "unknown", "--config", config]), 2)
            self.assertEqual(json.loads(output.getvalue())["outcome"], "configuration_error")


if __name__ == "__main__":
    unittest.main()
