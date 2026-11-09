"""Atomic local archives, per-attempt metadata, cache state, and process locks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from election_pipeline.settings import Source


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def member_path(raw_dir: Path, digest: str) -> Path:
    """Where a content-addressed bundle member lives beside its source's archives."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid bundle member SHA-256")
    return Path(raw_dir) / "members" / f"{digest}.bin"


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".json-", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def replace_private(path: Path, text: str) -> None:
    """Atomically replace a configuration file; the new file is readable by its owner only."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".private-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class CollectionLocked(RuntimeError):
    """Another process currently owns this source's collection lock."""


class ArchiveStore:
    def __init__(self, root: Path, source: Source):
        self.root = root.resolve()
        self.source = source
        self.raw_dir = self.root / "raw" / source.state / source.election_date / source.source_id
        self.state_path = self.root / "state" / f"{source.source_id}.json"

    @contextmanager
    def lock(self, *, stage: str = "collection") -> Iterator[None]:
        if stage not in ("collection", "pipeline"):
            raise ValueError("Unknown lock stage")

        suffix = "" if stage == "collection" else ".pipeline"
        lock_path = self.root / "locks" / f"{self.source.source_id}{suffix}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise CollectionLocked(self.source.source_id) from error
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise CollectionLocked(self.source.source_id) from error
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError(f"Unsupported or corrupt collector state: {self.state_path}")
        return value

    def write_state(self, value: dict) -> None:
        atomic_json(self.state_path, {**value, "schema_version": 1})

    def write_attempt(self, record: dict) -> Path:
        path = (
            self.root
            / "metadata"
            / self.source.source_id
            / record["started_at"][:10]
            / f"{record['fetch_id']}.json"
        )
        atomic_json(path, record)
        return path

    def temporary_download(self) -> Path:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=self.raw_dir)
        os.close(descriptor)
        return Path(name)

    def remove_abandoned_downloads(self) -> None:
        # Caller holds the OS lock, so none belongs to another active source run.
        if self.raw_dir.exists():
            for path in self.raw_dir.glob(".download-*.part"):
                path.unlink()
            for path in self.raw_dir.glob(".bundle-*"):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)

    def archive_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid cached SHA-256")
        suffix = "zip" if self.source.format == "bundle" else self.source.format
        return self.raw_dir / f"{digest}.{suffix}"

    def cached_archive(self, state: dict) -> Path | None:
        if not state.get("sha256"):
            return None
        path = self.archive_path(state["sha256"])
        if path.is_file() and file_hash(path) == state["sha256"]:
            return path
        return None

    def commit_download(self, temporary: Path, digest: str) -> tuple[Path, bool]:
        target = self.archive_path(digest)
        if target.exists():
            if file_hash(target) != digest:
                # Do not silently replace a corrupt historical archive.
                raise OSError(f"Existing archive failed its content hash: {target}")
            temporary.unlink()
            return target, False
        os.replace(temporary, target)
        return target, True

    def store_member(self, raw: bytes) -> str:
        """Keep one copy of a bundle member under ``members/<sha256>.bin``; return its digest.

        County bundles reference members by digest, so a report polled unchanged a hundred
        times a day is stored once while every distinct version is retained.
        """
        digest = hashlib.sha256(raw).hexdigest()
        target = member_path(self.raw_dir, digest)
        if target.exists():
            if target.is_symlink() or file_hash(target) != digest:
                raise OSError(f"Existing bundle member failed its content hash: {target}")
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".member-", suffix=".part", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)
        return digest

    def quarantine(self, temporary: Path, digest: str) -> Path:
        target = self.root / "quarantine" / self.source.source_id / f"{digest}.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_hash(target) != digest:
                raise OSError(f"Existing quarantine file failed its content hash: {target}")
            temporary.unlink()
        else:
            os.replace(temporary, target)
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()
