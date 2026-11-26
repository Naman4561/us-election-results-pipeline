"""Per-host TLS compatibility never disables ordinary certificate verification."""

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from election_pipeline import elections, tls
from election_pipeline.collect import FetchFailure, fetch_file
from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.tls import context_for_url

ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.elections.alaska.gov/enr/26GENR/results/GA_ENR_Precinct_State_of_Alaska.csv"
FLORIDA = "https://flelectionfiles.floridados.gov/enightfilespublic/20261103_ElecResultsFL.txt"
# GoDaddy TLS Root CA - R1, as GoDaddy's repository lists its thumbprint.
FLORIDA_ROOT_DER_SHA256 = "25cf3da8e9b97addbf92543c2b82527c8a4e2cff2062a6483040d4b64ace719f"
# What the election definition declares, as reviewed: a change to a declaration is a change
# to a trust decision, so it must change this test too.
ISSUERS_SHA256 = "d843cd9900279123e130f0c9ac443451f5a9963254c7f199d61df0e8628f0aa0"
FLORIDA_ROOT_SHA256 = "471c59d5851cb2be394bce8fc814301233cdd7a92e513d8a3360dfe52965cc62"
LEGACY_CBC_SUITES = ("ECDHE-RSA-AES256-SHA", "ECDHE-RSA-AES128-SHA")


def tampered(host):
    """The declarations with ``host``'s certificate hash wrong, as a swapped file would be."""
    from dataclasses import replace

    return {h: replace(i, sha256="0" * 64) if h == host else i for h, i in tls.declared().items()}


class DeclarationTests(unittest.TestCase):
    def test_the_election_declares_exactly_the_reviewed_exceptions(self):
        found = {(i.kind, i.hosts, i.certificate, i.sha256, i.suites) for i in elections.load().tls}
        self.assertEqual(
            found,
            {
                (
                    "issuer_bundle",
                    ("elections.alaska.gov", "www.elections.alaska.gov"),
                    "alaska-issuers.pem",
                    ISSUERS_SHA256,
                    (),
                ),
                ("legacy_cbc", ("www.co.hinds.ms.us",), "", "", LEGACY_CBC_SUITES),
                (
                    "trusted_root",
                    ("flelectionfiles.floridados.gov",),
                    "florida-root.pem",
                    FLORIDA_ROOT_SHA256,
                    (),
                ),
            },
        )
        for item in elections.load().tls:
            self.assertGreater(len(item.reason), 40)

    def test_a_declaration_is_checked_as_it_loads(self):
        import shutil as copying
        import tempfile as temporary_files

        from election_pipeline.elections import TlsException

        base = {"kind": "trusted_root", "hosts": ("a.example.gov",), "reason": "because it is so"}
        good = {**base, "certificate": "x.pem", "sha256": "a" * 64}
        self.assertEqual(TlsException(**good).hosts, ("a.example.gov",))
        for changes, message in (
            ({"kind": "wildcard"}, "is one of"),
            ({"hosts": ()}, "exact hosts"),
            ({"hosts": ("*.example.gov",)}, "exact hosts"),
            ({"hosts": ("A.example.gov",)}, "lower case"),
            ({"reason": " "}, "say why"),
            ({"sha256": ""}, "a certificate and its SHA-256"),
            ({"sha256": "xyz"}, "64 hexadecimal"),
            ({"suites": ("ECDHE-RSA-AES256-SHA",)}, "only legacy_cbc"),
        ):
            with self.subTest(changes), self.assertRaisesRegex(ValueError, message):
                TlsException(**{**good, **changes})
        legacy = {"kind": "legacy_cbc", "hosts": ("old.example.gov",), "reason": "old server"}
        with self.assertRaisesRegex(ValueError, "only legacy_cbc"):
            TlsException(**legacy)
        with self.assertRaisesRegex(ValueError, "forward-secret"):
            TlsException(**legacy, suites=("AES256-SHA",))
        # A host in two declarations is refused when the election loads.
        with temporary_files.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            copying.copytree(elections.CONFIG, config)
            head = config / "elections/2026-general/election.toml"
            head.write_text(
                head.read_text(encoding="utf-8")
                + '\n[[tls]]\nkind = "legacy_cbc"\nhosts = ["www.co.hinds.ms.us"]\n'
                + 'suites = ["ECDHE-RSA-AES128-SHA"]\nreason = "a second declaration"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "one TLS exception only"):
                elections.load(config=config)


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
                patch.object(tls, "declared", return_value=tampered("www.elections.alaska.gov")),
                patch("election_pipeline.collect.open_url") as request,
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
                    "election_pipeline.collect.open_url",
                    side_effect=HTTPError(url, 404, "Missing", {}, None),
                ) as request:
                    result = fetch_file(
                        source, CollectorSettings(), Path(temporary) / "download", {}, url=url
                    )
                self.assertEqual(result.status, 404)
                self.assertEqual("context" in request.call_args.kwargs, custom)


class FloridaTLSTests(unittest.TestCase):
    def test_the_packaged_root_is_godaddys_and_verification_stays_enabled(self):
        pem = (ROOT / "src/election_pipeline/resources/florida-root.pem").read_bytes()
        self.assertEqual(hashlib.sha256(pem).hexdigest(), FLORIDA_ROOT_SHA256)
        self.assertEqual(pem.count(b"-----BEGIN CERTIFICATE-----"), 1)
        der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
        self.assertEqual(hashlib.sha256(der).hexdigest(), FLORIDA_ROOT_DER_SHA256)
        context = context_for_url(FLORIDA)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIn(der, context.get_ca_certs(binary_form=True))

    def test_only_the_florida_file_host_trusts_the_root(self):
        from election_pipeline.settings import load_config

        _, sources = load_config(ROOT / "config")
        self.assertEqual(sources["fl_2026_general"].url, FLORIDA)
        for url in (
            "https://floridados.gov/",
            "https://www.floridados.gov/",
            "https://flelectionfiles.floridados.gov.example.com/",
            "https://example.com/?host=flelectionfiles.floridados.gov",
            "https://floridaelectionwatch.gov/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(context_for_url(url))

    def test_bad_root_fails_before_http_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Source("fl_probe", "FL", "2026-11-03", FLORIDA)
            with (
                patch.object(
                    tls, "declared", return_value=tampered("flelectionfiles.floridados.gov")
                ),
                patch("election_pipeline.collect.open_url") as request,
                self.assertRaisesRegex(FetchFailure, "integrity") as failure,
            ):
                fetch_file(source, CollectorSettings(), Path(temporary) / "download", {})
            self.assertFalse(failure.exception.retryable)
            request.assert_not_called()

    def test_an_untrusted_certificate_still_fails(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cert = make_test_certificate(Path(temporary.name), self)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert)
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake(context_for_url(FLORIDA), server)


# Python's built-in default cipher string, used by the official python:3.12 image.
# Distribution builds (e.g. Ubuntu) may use OpenSSL's broader list instead.
PYTHON_DEFAULT_CIPHERS = (
    "@SECLEVEL=2:ECDH+AESGCM:ECDH+CHACHA20:ECDH+AES:DHE+AES:!aNULL:!eNULL:!aDSS:!SHA1:!AESCCM"
)
HINDS = "https://www.co.hinds.ms.us/pgs/results/Unofficial%20Summary%20Report.pdf"


def strict_default_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    context.set_ciphers(PYTHON_DEFAULT_CIPHERS)
    return context


def names(context):
    return {c["name"] for c in context.get_ciphers()}


def handshake(client, server):
    """One TLS handshake with a local server; returns the negotiated cipher."""
    with socket.create_server(("127.0.0.1", 0)) as listener:

        def serve():
            connection, _ = listener.accept()
            try:
                server.wrap_socket(connection, server_side=True).close()
            except (ssl.SSLError, OSError):
                connection.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=5) as raw:
                with client.wrap_socket(raw, server_hostname="localhost") as tls:
                    return tls.cipher()[0]
        finally:
            thread.join(5)


def make_test_certificate(directory, test):
    """A throwaway localhost certificate and key in one PEM file, made by the openssl CLI.

    The subject comes from a config file so no argument starts with "/": Git's MSYS
    openssl on Windows would otherwise rewrite "/CN=..." into a filesystem path.
    """
    if not shutil.which("openssl"):
        test.skipTest("openssl CLI is needed to create a throwaway test certificate")
    config, key, cert = (directory / name for name in ("req.cnf", "key.pem", "cert.pem"))
    config.write_text(
        "[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
        "[dn]\nCN = localhost\n[ext]\nsubjectAltName = DNS:localhost\n",
        encoding="ascii",
    )
    created = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(config),
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"},
    )
    if created.returncode:
        test.skipTest("openssl could not create a test certificate: " + created.stderr.strip())
    combined = directory / "localhost.pem"
    combined.write_bytes(key.read_bytes() + cert.read_bytes())
    return combined


class HindsTLSTests(unittest.TestCase):
    def hinds_context(self):
        # Reproduce the official image, whose Python default excludes SHA-1 suites.
        with patch("election_pipeline.tls.ssl.create_default_context", strict_default_context):
            return context_for_url(HINDS)

    def test_only_two_forward_secret_suites_are_added_and_verification_stays(self):
        context = self.hinds_context()
        baseline = names(strict_default_context())
        self.assertFalse(baseline & set(LEGACY_CBC_SUITES))
        self.assertEqual(names(context) - baseline, set(LEGACY_CBC_SUITES))
        self.assertFalse({"AES256-SHA", "AES128-SHA"} & names(context))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_only_the_configured_hinds_host_is_affected(self):
        from election_pipeline.parsers import ms

        counties = ms.policy()["counties"]
        hinds = next(c for c in counties if c["key"] == "HINDS")
        self.assertEqual(hinds["url"], HINDS)
        self.assertIsNotNone(context_for_url(hinds["url"]))
        others = [c["url"] for c in counties if c.get("url") and c["key"] != "HINDS"]
        for url in (
            "https://co.hinds.ms.us/pgs/results/x.pdf",
            "https://www.co.hinds.ms.us.example.com/x.pdf",
            "https://example.com/?host=www.co.hinds.ms.us",
            *others,
        ):
            with self.subTest(url=url):
                self.assertIsNone(context_for_url(url))

    def test_handshake_with_a_server_offering_only_the_hinds_suite(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cert = make_test_certificate(Path(temporary.name), self)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(cert)
        server.maximum_version = ssl.TLSVersion.TLSv1_2
        server.set_ciphers("ECDHE-RSA-AES256-SHA")

        strict = strict_default_context()
        strict.load_verify_locations(cert)
        with self.assertRaises((ssl.SSLError, OSError)):
            handshake(strict, server)  # What production's container saw.

        trusted = self.hinds_context()
        trusted.load_verify_locations(cert)
        self.assertEqual(handshake(trusted, server), "ECDHE-RSA-AES256-SHA")

        # Certificate verification is unchanged: an untrusted certificate fails.
        with self.assertRaises(ssl.SSLCertVerificationError):
            handshake(self.hinds_context(), server)
