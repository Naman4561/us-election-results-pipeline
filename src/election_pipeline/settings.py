"""Validated public-source configuration; no source URLs are embedded in the downloader."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from election_pipeline import __version__


def validate_url(url: str) -> None:
    parts = urlsplit(url)
    _ = parts.port  # Validate an explicitly supplied port before starting a request.
    local_test = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parts.hostname or (parts.scheme != "https" and not local_test):
        raise ValueError("Sources require HTTPS (HTTP is allowed only on loopback for tests)")
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Source URLs cannot contain credentials or fragments")


@dataclass(frozen=True)
class CollectorSettings:
    timeout_seconds: float = 30
    max_download_seconds: float = 60
    max_attempts: int = 3
    backoff_seconds: float = 2
    max_retry_wait_seconds: float = 30
    max_download_bytes: int = 50 * 1024 * 1024
    max_uncompressed_bytes: int = 256 * 1024 * 1024
    max_zip_members: int = 20
    user_agent: str = "US-Election-Results-Pipeline/0.1"

    def __post_init__(self) -> None:
        for name in (
            "timeout_seconds",
            "max_download_seconds",
            "backoff_seconds",
            "max_retry_wait_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0 and 120 seconds")
        if not 0 < self.max_download_seconds <= 300:
            raise ValueError("max_download_seconds must be between 0 and 300 seconds")
        for name in (
            "max_attempts",
            "max_download_bytes",
            "max_uncompressed_bytes",
            "max_zip_members",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_attempts > 5:
            raise ValueError("max_attempts cannot exceed 5")
        if self.backoff_seconds > 30 or self.max_retry_wait_seconds > 60:
            raise ValueError("Retry waits must remain bounded: backoff <=30, max wait <=60 seconds")
        if (
            not isinstance(self.user_agent, str)
            or not self.user_agent
            or "\n" in self.user_agent
            or "\r" in self.user_agent
        ):
            raise ValueError("user_agent must be a nonempty, single-line string")


@dataclass(frozen=True)
class Source:
    source_id: str
    state: str
    election_date: str
    url: str
    format: str = "zip"
    expected_members: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", self.source_id):
            raise ValueError("source_id must be a lowercase identifier of at most 80 characters")
        if not re.fullmatch(r"[A-Z]{2}", self.state):
            raise ValueError("state must be a two-letter uppercase code")
        if date.fromisoformat(self.election_date).isoformat() != self.election_date:
            raise ValueError("election_date must use YYYY-MM-DD")
        validate_url(self.url)
        if self.format != "zip":
            raise ValueError("Only ZIP validation is implemented in this collection milestone")
        if not isinstance(self.expected_members, tuple) or not all(
            isinstance(name, str) and name for name in self.expected_members
        ):
            raise ValueError("expected_members must be a tuple of nonempty member names")

    def signature(self, settings: CollectorSettings) -> str:
        # HTTP validators are reusable only for this URL and these validation rules.
        contract = {
            "source": asdict(self),
            "validator_version": __version__,
            "max_download_bytes": settings.max_download_bytes,
            "max_uncompressed_bytes": settings.max_uncompressed_bytes,
            "max_zip_members": settings.max_zip_members,
        }
        return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def load_config(path: Path) -> tuple[CollectorSettings, dict[str, Source]]:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    if set(config) - {"collector", "sources"}:
        raise ValueError("Top-level configuration keys must be collector and sources")
    settings = CollectorSettings(**config.get("collector", {}))
    sources: dict[str, Source] = {}
    for source_id, raw in config.get("sources", {}).items():
        allowed = {field.name for field in fields(Source)} - {"source_id"}
        if set(raw) - allowed:
            raise ValueError(
                f"Unknown source settings for {source_id}: {sorted(set(raw) - allowed)}"
            )
        item = dict(raw)
        if isinstance(item.get("election_date"), date):
            item["election_date"] = item["election_date"].isoformat()
        names = item.get("expected_members", [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"expected_members must be an array of strings: {source_id}")
        item["expected_members"] = tuple(names)
        sources[source_id] = Source(source_id=source_id, **item)
    if not sources:
        raise ValueError("Configure at least one source")
    return settings, sources
