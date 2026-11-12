"""A source can prefer IPv6 (Virginia: its CDN refuses the server's IPv4 address)."""

import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from election_pipeline import collect
from election_pipeline.settings import CollectorSettings, Source, load_config

ROOT = Path(__file__).resolve().parents[1]
V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))


class IPv6FirstTests(unittest.TestCase):
    def connect(self, addresses, refused=()):
        tried = []

        def make(family, *args):
            sock = MagicMock()

            def connect(target):
                tried.append(target[0])
                if target[0] in refused:
                    raise ConnectionRefusedError(target[0])

            sock.connect.side_effect = connect
            return sock

        with (
            patch.object(collect.socket, "getaddrinfo", return_value=addresses),
            patch.object(collect.socket, "socket", side_effect=make),
        ):
            try:
                collect.connect_ipv6_first(("example.org", 443), 5)
            except OSError:
                pass
        return tried

    def test_ipv6_addresses_are_tried_first_and_ipv4_stays_the_fallback(self):
        # The system puts IPv4 first when a container has only a private IPv6 address.
        self.assertEqual(self.connect([V4, V6]), ["2001:db8::1"])
        self.assertEqual(
            self.connect([V4, V6], refused={"2001:db8::1"}), ["2001:db8::1", "192.0.2.1"]
        )
        self.assertEqual(self.connect([V4]), ["192.0.2.1"])  # no IPv6 address: as before

    def test_only_a_source_that_asks_for_it_connects_ipv6_first(self):
        settings = CollectorSettings()
        source = Source("xx_test", "XX", "2026-11-03", "https://example.org/data.zip")
        for prefer in (False, True):
            with (
                patch.object(collect, "open_url", side_effect=OSError("stop")) as opened,
                tempfile.TemporaryDirectory() as root,
                self.assertRaises(collect.FetchFailure),
            ):
                collect.fetch_file(
                    replace(source, prefer_ipv6=prefer), settings, Path(root) / "f", {}
                )
            self.assertEqual(opened.call_args.kwargs.get("prefer_ipv6", False), prefer)
        handler = collect.IPv6FirstHTTPSHandler()
        self.assertIsInstance(handler, collect.HTTPSHandler)

    def test_the_setting_is_a_boolean_and_changes_only_its_own_source_signature(self):
        settings = CollectorSettings()
        source = Source("xx_test", "XX", "2026-11-03", "https://example.org/data.zip")
        self.assertNotEqual(
            source.signature(settings), replace(source, prefer_ipv6=True).signature(settings)
        )
        with self.assertRaisesRegex(ValueError, "prefer_ipv6"):
            replace(source, prefer_ipv6="yes")

    def test_virginia_alone_prefers_ipv6(self):
        _, sources = load_config(ROOT / "config/sources.toml")
        self.assertEqual({n for n, s in sources.items() if s.prefer_ipv6}, {"va_2026_general"})


if __name__ == "__main__":
    unittest.main()
