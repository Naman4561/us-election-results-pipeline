"""Complete Alaska's served issuer chain using reviewed, packaged public certificates."""

import hashlib
import ssl
from importlib.resources import files
from urllib.parse import urlsplit

ALASKA_HOSTS = {"elections.alaska.gov", "www.elections.alaska.gov"}
ISSUERS_SHA256 = "d843cd9900279123e130f0c9ac443451f5a9963254c7f199d61df0e8628f0aa0"


def context_for_url(url):
    if urlsplit(url).hostname not in ALASKA_HOSTS:
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
