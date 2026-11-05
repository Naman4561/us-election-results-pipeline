"""Alaska's missing issuer chain never disables ordinary certificate verification."""

import hashlib
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from election_pipeline.collect import FetchFailure, fetch_file
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.tls import ISSUERS_SHA256, context_for_url

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.elections.alaska.gov/enr/26GENR/results/GA_ENR_Precinct_State_of_Alaska.csv"


class AlaskaTLSTests(unittest.TestCase):
    def test_issuer_bundle_is_packaged_and_verification_stays_enabled(self):
        pem = (ROOT / "src/election_pipeline/resources/alaska-issuers.pem").read_bytes()
        self.assertEqual(hashlib.sha256(pem).hexdigest(), ISSUERS_SHA256)
        self.assertEqual(pem.count(b"-----BEGIN CERTIFICATE-----"), 2)
        for host in ("www.elections.alaska.gov", "elections.alaska.gov"):
            context = context_for_url("https://" + host + "/")
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertFalse(context.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0))

    def test_other_hosts_use_the_existing_default_transport(self):
        for url in (
            "https://s3.amazonaws.com/dl.ncsbe.gov/",
            "https://elections.alaska.gov.example.com/",
            "https://example.com/?host=elections.alaska.gov",
        ):
            self.assertIsNone(context_for_url(url))

    def test_bad_bundle_fails_before_http_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Source("ak_probe", "AK", "2026-11-03", URL)
            with (
                patch("election_pipeline.tls.ISSUERS_SHA256", "0" * 64),
                patch("election_pipeline.collect.urlopen") as request,
                self.assertRaisesRegex(FetchFailure, "integrity") as failure,
            ):
                fetch_file(source, CollectorSettings(), Path(temporary) / "download", {})
            self.assertFalse(failure.exception.retryable)
            request.assert_not_called()

    def test_fetch_uses_the_context_for_the_actual_bundle_component_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Source("ak_probe", "AK", "2026-11-03", URL)
            for url, custom in ((URL, True), ("https://example.com/index.html", False)):
                with patch(
                    "election_pipeline.collect.urlopen",
                    side_effect=HTTPError(url, 404, "Missing", {}, None),
                ) as request:
                    result = fetch_file(
                        source, CollectorSettings(), Path(temporary) / "download", {}, url=url
                    )
                self.assertEqual(result.status, 404)
                self.assertEqual("context" in request.call_args.kwargs, custom)
