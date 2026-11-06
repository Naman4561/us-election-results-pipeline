"""Versioned research files and typed tables in one local SQLite database."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from election_pipeline.research.metadata import clean_metadata
from election_pipeline.storage import utc_now

MAX_BYTES = 256 * 1024 * 1024
CHUNK = 128 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
INTEGER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)\Z")
NUMBER = re.compile(r"[+-]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")
IDENTIFIER = re.compile(
    r"(?:^|[^a-z0-9])(?:fips[0-9]*|geoid[0-9]*|code|id|zip[0-9]*)(?:$|[^a-z0-9])", re.I
)
SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "number": "REAL"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    content BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    identity TEXT NOT NULL UNIQUE,
    dataset_id TEXT NOT NULL,
    metadata TEXT NOT NULL,
    columns TEXT NOT NULL,
    row_count INTEGER,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_dataset ON versions(dataset_id, id DESC);
CREATE TABLE IF NOT EXISTS version_files (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    position INTEGER NOT NULL,
    filename TEXT NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY(version_id, position)
);
"""


def dump(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def typed(value: str, kind: str):
    if kind == "text":
        return value
    if kind == "integer" and INTEGER.fullmatch(value):
        result = int(value)
        if abs(result) <= MAX_SAFE_INTEGER:
            return result
    if kind == "number" and NUMBER.fullmatch(value):
        result = float(value)
        if math.isfinite(result):
            return result
    raise ValueError(f"Invalid {kind} value")


def infer(value: str) -> str:
    if INTEGER.fullmatch(value):
        # Longer integers are usually identifiers or exceed exact browser precision.
        if len(value.lstrip("+-")) <= 16 and abs(int(value)) <= MAX_SAFE_INTEGER:
            return "integer"
        return "text"
    if NUMBER.fullmatch(value) and math.isfinite(float(value)):
        return "number"
    return "text"


def stage(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    if not source.is_file():
        raise ValueError(f"Input is not a regular file: {source.name}")
    with source.open("rb") as original, destination.open("wb") as copy:
        for chunk in iter(lambda: original.read(CHUNK), b""):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError(f"{source.name} exceeds the 256 MiB per-file limit")
            digest.update(chunk)
            copy.write(chunk)
    return digest.hexdigest(), size


@contextmanager
def table_rows(path: Path, delimiter: str):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter, strict=True)
        columns = [name.strip() for name in next(reader, ())]
        if (
            not 1 <= len(columns) <= 256
            or any(not name or len(name) > 160 for name in columns)
            or len(columns) != len(set(columns))
        ):
            raise ValueError("Use 1 to 256 unique, nonempty column names of up to 160 characters")

        def rows():
            for row in reader:
                if not row:
                    continue
                if len(row) != len(columns):
                    raise ValueError(f"Line {reader.line_num} has the wrong column count")
                yield reader.line_num, row

        yield columns, rows()


def inspect_table(path: Path, delimiter: str, overrides: dict) -> list[dict]:
    with table_rows(path, delimiter) as (names, rows):
        if unknown := overrides.keys() - set(names):
            raise ValueError(f"Unknown column_types names: {', '.join(sorted(unknown))}")
        kinds = [
            overrides.get(name) or ("text" if IDENTIFIER.search(name) else None) for name in names
        ]
        for _, row in rows:
            for index, value in enumerate(row):
                if not value or names[index] in overrides or kinds[index] == "text":
                    continue
                kind = infer(value)
                if kinds[index] is None or kind != "integer":
                    kinds[index] = kind

    return [{"name": name, "type": kind or "text"} for name, kind in zip(names, kinds)]


def add_rows(db, version_id, path, delimiter, columns) -> int:
    definitions = ", ".join(f"c{i} {SQL_TYPES[c['type']]}" for i, c in enumerate(columns))
    db.execute(f"CREATE TABLE rows_{version_id} ({definitions})")
    slots = ", ".join("?" for _ in columns)

    def values():
        with table_rows(path, delimiter) as (_, rows):
            for line, row in rows:
                converted = []
                for cell, column in zip(row, columns):
                    try:
                        converted.append(typed(cell, column["type"]) if cell else None)
                    except ValueError as error:
                        raise ValueError(f"Line {line}, {column['name']}: {error}") from error
                yield converted

    return db.executemany(f"INSERT INTO rows_{version_id} VALUES ({slots})", values()).rowcount


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        # A file download can resume on another worker thread between chunks.
        db = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_file(
        self,
        source: Path,
        metadata: dict,
        *,
        originals: tuple[Path, ...] = (),
        archive_only: bool = False,
        expected_hashes: dict[Path, str] | None = None,
    ) -> dict:
        metadata = clean_metadata(metadata)
        if len(originals) > 16:
            raise ValueError("A version can have up to 16 original attachments")
        suffix = source.suffix.lower()
        if archive_only:
            if metadata.get("column_types"):
                raise ValueError("Column types require a tabular import")
        elif suffix not in {".csv", ".tsv"}:
            raise ValueError("Use CSV or TSV for analysis. Use --archive-only for other formats")
        delimiter = "\t" if suffix == ".tsv" else ","

        with TemporaryDirectory(prefix=".import-", dir=self.root) as temporary:
            paths = []
            for index, original in enumerate((source, *originals)):
                staged = Path(temporary) / str(index)
                digest, size = stage(original, staged)
                expected = (expected_hashes or {}).get(original)
                if expected is not None and digest != expected:
                    raise ValueError(f"{original.name} SHA-256 differs from the expected snapshot")
                paths.append((original.name, staged, digest, size))

            identity = hashlib.sha256(
                dump(
                    [metadata, archive_only, [(name, digest) for name, _, digest, _ in paths]]
                ).encode("utf-8")
            ).hexdigest()
            columns = (
                []
                if archive_only
                else inspect_table(paths[0][1], delimiter, metadata.get("column_types", {}))
            )

            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT id, row_count FROM versions WHERE identity = ?", (identity,)
                ).fetchone()
                if existing:
                    return {
                        "outcome": "already_imported",
                        "version_id": existing["id"],
                        "row_count": existing["row_count"],
                    }
                previous = db.execute(
                    """SELECT metadata FROM versions WHERE dataset_id = ?
                       ORDER BY id DESC LIMIT 1""",
                    (metadata["dataset_id"],),
                ).fetchone()
                if previous:
                    prior = json.loads(previous["metadata"])
                    if any(
                        prior[key] != metadata[key] for key in ("kind", "jurisdiction", "geography")
                    ):
                        raise ValueError("Use a different dataset_id for a different data scope")
                version_id = db.execute(
                    """INSERT INTO versions (identity, dataset_id, metadata, columns, imported_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (identity, metadata["dataset_id"], dump(metadata), dump(columns), utc_now()),
                ).lastrowid
                row_count = None
                if columns:
                    row_count = add_rows(db, version_id, paths[0][1], delimiter, columns)
                    db.execute(
                        "UPDATE versions SET row_count = ? WHERE id = ?", (row_count, version_id)
                    )

                for position, (name, staged, digest, size) in enumerate(paths):
                    file_id = self._store_file(db, staged, digest, size)
                    db.execute(
                        "INSERT INTO version_files VALUES (?, ?, ?, ?)",
                        (version_id, position, name, file_id),
                    )

        return {"outcome": "imported", "version_id": version_id, "row_count": row_count}

    @staticmethod
    def _store_file(db, path, digest, size) -> int:
        existing = db.execute("SELECT id FROM files WHERE sha256 = ?", (digest,)).fetchone()
        if existing:
            return existing["id"]
        file_id = db.execute(
            "INSERT INTO files (sha256, size, content) VALUES (?, ?, zeroblob(?))",
            (digest, size, size),
        ).lastrowid
        with path.open("rb") as source, db.blobopen("files", "content", file_id) as blob:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                blob.write(chunk)
        return file_id

    def versions(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, metadata, row_count, imported_at FROM versions ORDER BY id DESC"
            )
            return [self._version(row) for row in rows]

    @staticmethod
    def _version(row) -> dict:
        return {
            "id": row["id"],
            "row_count": row["row_count"],
            "imported_at": row["imported_at"],
            **json.loads(row["metadata"]),
        }

    def version(self, version_id: int) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown dataset version")
            files = db.execute(
                """SELECT v.position, v.filename, f.sha256, f.size
                   FROM version_files v JOIN files f ON f.id = v.file_id
                   WHERE v.version_id = ? ORDER BY v.position""",
                (version_id,),
            )
            return {
                **self._version(row),
                "columns": json.loads(row["columns"]),
                "files": [dict(file) for file in files],
            }

    def file(self, version_id: int, position: int) -> tuple[int, str, int]:
        with self.connect() as db:
            row = db.execute(
                """SELECT f.id, v.filename, f.size
                   FROM version_files v JOIN files f ON f.id = v.file_id
                   WHERE v.version_id = ? AND v.position = ?""",
                (version_id, position),
            ).fetchone()
            if row is None:
                raise KeyError("Unknown file")
            return tuple(row)

    def file_chunks(self, file_id: int):
        with self.connect() as db, db.blobopen("files", "content", file_id, readonly=True) as blob:
            yield from iter(lambda: blob.read(CHUNK), b"")
