"""Validated public-source configuration; no source URLs are embedded in the downloader."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from dataclasses import asdict, dataclass, fields, replace
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
    max_csv_rows: int = 2_000_000
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
            "max_csv_rows",
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
    expected_columns: tuple[str, ...] = ()
    dataset: str = "election_results"
    min_interval_seconds: int = 0
    allow_missing: bool = False
    files: tuple[tuple[str, str], ...] = ()
    primary_filename: str = "results.xml"
    contests: tuple[str, ...] = ()
    supplemental_members: tuple[str, ...] = ()
    collector_overrides: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", self.source_id):
            raise ValueError("source_id must be a lowercase identifier of at most 80 characters")

        if not re.fullmatch(r"[A-Z]{2}", self.state):
            raise ValueError("state must be a two-letter uppercase code")

        if date.fromisoformat(self.election_date).isoformat() != self.election_date:
            raise ValueError("election_date must use YYYY-MM-DD")

        validate_url(self.url)

        if self.format not in {"zip", "csv", "bundle"}:
            raise ValueError("Supported formats are zip, csv and bundle")

        if not isinstance(self.expected_members, tuple) or not all(
            isinstance(name, str) and name for name in self.expected_members
        ):
            raise ValueError("expected_members must be a tuple of nonempty member names")

        if not isinstance(self.expected_columns, tuple) or not all(
            isinstance(name, str) and name.strip() for name in self.expected_columns
        ):
            raise ValueError("expected_columns must be a tuple of nonempty column names")

        if self.format == "csv" and (not self.expected_columns or self.expected_members):
            raise ValueError("CSV sources need expected_columns and cannot specify ZIP members")

        if self.format in {"zip", "bundle"} and self.expected_columns:
            raise ValueError("ZIP member row checks belong to a later source parser")

        if not isinstance(self.files, tuple):
            raise ValueError("files must be a tuple of filename/URL pairs")
        names = []
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", self.primary_filename):
            raise ValueError("Invalid primary bundle filename")
        for entry in self.files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("files must be filename/URL pairs")
            name, url = entry
            if not isinstance(name, str) or not isinstance(url, str):
                raise ValueError("files must contain strings")
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", name) or name == self.primary_filename:
                raise ValueError("Invalid bundle reference filename")
            validate_url(url)
            names.append(name)
        if len(names) != len(set(names)) or (self.files and self.format != "bundle"):
            raise ValueError("Reference files require bundle format and unique names")
        if (
            not isinstance(self.supplemental_members, tuple)
            or any(
                not isinstance(n, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", n)
                or n == self.primary_filename
                or n in names
                for n in self.supplemental_members
            )
            or len(set(self.supplemental_members)) != len(self.supplemental_members)
        ):
            raise ValueError("Supplemental members must be distinct safe filenames")
        if self.supplemental_members and self.format != "bundle":
            raise ValueError("Only bundles support supplemental members")
        if self.format == "bundle" and set(self.expected_members) != {
            self.primary_filename,
            *names,
            *self.supplemental_members,
        }:
            raise ValueError("Bundle members must match its primary file and reference files")

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", self.dataset):
            raise ValueError("dataset must be a lowercase identifier")

        if (
            not isinstance(self.contests, tuple)
            or any(
                not isinstance(contest, str)
                or not re.fullmatch(self.state + r"-(?:SEN|GOV|AL|[0-9]{2})", contest)
                for contest in self.contests
            )
            or len(self.contests) != len(set(self.contests))
        ):
            raise ValueError("contests must contain distinct race IDs for this state")

        if type(self.min_interval_seconds) is not int or self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be a nonnegative integer")

        if type(self.allow_missing) is not bool:
            raise ValueError("allow_missing must be true or false")
        if (
            not isinstance(self.collector_overrides, tuple)
            or any(
                not isinstance(p, tuple)
                or len(p) != 2
                or p[0] not in {"max_download_bytes", "max_uncompressed_bytes", "max_zip_members"}
                or type(p[1]) is not int
                or p[1] <= 0
                for p in self.collector_overrides
            )
            or len(dict(self.collector_overrides)) != len(self.collector_overrides)
        ):
            raise ValueError("Invalid per-source archive limits")

    def effective_settings(self, settings: CollectorSettings) -> CollectorSettings:
        return (
            replace(settings, **dict(self.collector_overrides))
            if self.collector_overrides
            else settings
        )

    def signature(self, settings: CollectorSettings) -> str:
        settings = self.effective_settings(settings)
        source_contract = asdict(self)
        for key in ("supplemental_members", "collector_overrides"):
            if not source_contract[key]:
                source_contract.pop(key)
        if not self.contests:
            source_contract.pop("contests")
        if self.primary_filename == "results.xml":
            source_contract.pop("primary_filename")
        contract = {
            "source": source_contract,
            "validator_version": __version__,
            "max_download_bytes": settings.max_download_bytes,
            "max_uncompressed_bytes": settings.max_uncompressed_bytes,
            "max_zip_members": settings.max_zip_members,
            "max_csv_rows": settings.max_csv_rows,
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
        for key in ("expected_members", "expected_columns", "contests", "supplemental_members"):
            names = item.get(key, [])

            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError(f"{key} must be an array of strings: {source_id}")

            item[key] = tuple(names)
        files = item.get("files", {})
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in files.items()
        ):
            raise ValueError("files must map filenames to public URLs")
        item["files"] = tuple(sorted(files.items()))
        overrides = item.get("collector_overrides", {})
        if not isinstance(overrides, dict):
            raise ValueError("collector_overrides must be a table")
        item["collector_overrides"] = tuple(sorted(overrides.items()))
        sources[source_id] = Source(source_id=source_id, **item)
    if not sources:
        raise ValueError("Configure at least one source")
    return settings, sources
