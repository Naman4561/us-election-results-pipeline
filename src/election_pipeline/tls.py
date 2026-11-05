"""Per-host TLS compatibility that never weakens certificate or hostname verification.

- Alaska serves an incomplete issuer chain; reviewed, packaged issuer certificates
  complete it.
- Hinds County's IIS server negotiates only ECDHE-RSA CBC suites with SHA-1 MACs.
  Python's default cipher list excludes them, so the server resets the connection.
  Only that exact host gets those two forward-secret suites added back.
"""

import hashlib
import ssl
from importlib.resources import files
from urllib.parse import urlsplit

ALASKA_HOSTS = {"elections.alaska.gov", "www.elections.alaska.gov"}
ISSUERS_SHA256 = "d843cd9900279123e130f0c9ac443451f5a9963254c7f199d61df0e8628f0aa0"
LEGACY_CBC_HOSTS = {"www.co.hinds.ms.us"}
LEGACY_CBC_SUITES = ("ECDHE-RSA-AES256-SHA", "ECDHE-RSA-AES128-SHA")


def legacy_cbc_context():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Keep Python's own TLS 1.2 list and append only the two named suites; TLS 1.3
    # suites are configured separately and stay unchanged.
    tls12 = [c["name"] for c in context.get_ciphers() if c["protocol"] != "TLSv1.3"]
    context.set_ciphers(":".join(dict.fromkeys([*tls12, *LEGACY_CBC_SUITES])))
    return context


def context_for_url(url):
    host = urlsplit(url).hostname
    if host in LEGACY_CBC_HOSTS:
        return legacy_cbc_context()
    if host not in ALASKA_HOSTS:
        return None
    issuers = files("election_pipeline").joinpath("resources/alaska-issuers.pem").read_bytes()
    if hashlib.sha256(issuers).hexdigest() != ISSUERS_SHA256:
        raise ValueError("Alaska issuer bundle failed its integrity check")
    context = ssl.create_default_context()
    # These are issuer certificates, not additional trust anchors. Require a chain
    # to a root in the existing platform trust store, including on Python 3.13+.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    context.load_verify_locations(cadata=issuers.decode("ascii"))
    return context
