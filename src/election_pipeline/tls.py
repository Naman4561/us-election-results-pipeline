"""Per-host TLS compatibility that never weakens certificate or hostname verification.

The exceptions are declared in the election definition (``[[tls]]`` in ``election.toml``,
each with the exact hosts, what it adds and why), not in this module:

- ``issuer_bundle``: a server with an incomplete issuer chain (Alaska); reviewed, packaged
  issuer certificates complete it, and a chain to a root in the platform store is still
  required.
- ``legacy_cbc``: an old server that negotiates only ECDHE-RSA CBC suites with SHA-1 MACs
  (Hinds County). Python's default list excludes them, so the server resets the
  connection; only the named forward-secret suites are added back, for that exact host.
- ``trusted_root``: one reviewed, packaged root trusted beside the platform store, for a
  host whose chain ends in a root the image's trust store does not carry yet (Florida).

Only a host named in a declaration is affected, and every packaged certificate is checked
against its declared SHA-256 before use.
"""

import functools
import hashlib
import ssl
from importlib.resources import files
from urllib.parse import urlsplit

from election_pipeline import elections


@functools.lru_cache(maxsize=1)
def declared():
    """{host: the exception that names it}, from the election in force."""
    return {host: item for item in elections.load().tls for host in item.hosts}


def packaged(name, sha256, label):
    pem = files("election_pipeline").joinpath("resources/" + name).read_bytes()
    if hashlib.sha256(pem).hexdigest() != sha256:
        raise ValueError(f"{label} failed its integrity check")
    return pem.decode("ascii")


def legacy_cbc_context(suites):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Keep Python's own TLS 1.2 list and append only the named suites; TLS 1.3
    # suites are configured separately and stay unchanged.
    tls12 = [c["name"] for c in context.get_ciphers() if c["protocol"] != "TLSv1.3"]
    context.set_ciphers(":".join(dict.fromkeys([*tls12, *suites])))
    return context


def context_for_url(url):
    item = declared().get(urlsplit(url).hostname)
    if item is None:
        return None
    if item.kind == "legacy_cbc":
        return legacy_cbc_context(item.suites)
    pem = packaged(item.certificate, item.sha256, f"{item.certificate} for {item.hosts[0]}")
    context = ssl.create_default_context()
    if item.kind == "trusted_root":
        # A self-signed root, added beside the platform store; verification and the
        # hostname check are unchanged.
        context.load_verify_locations(cadata=pem)
        return context
    # These are issuer certificates, not additional trust anchors. Require a chain
    # to a root in the existing platform trust store, including on Python 3.13+.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    context.load_verify_locations(cadata=pem)
    return context
