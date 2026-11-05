"""Archive-level checks only. These do not validate election returns or vote totals."""

from __future__ import annotations

import csv
import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from election_pipeline.settings import CollectorSettings, Source


class InvalidPayload(ValueError):
    pass


class InvalidArchive(InvalidPayload):
    pass


def validate_archive(path: Path, source: Source, settings: CollectorSettings) -> list[dict]:
    settings = source.effective_settings(settings)
    if source.format not in {"zip", "bundle"}:
        raise InvalidArchive(f"No validator implemented for {source.format}")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > settings.max_zip_members:
                raise InvalidArchive("ZIP is empty or contains too many entries")
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise InvalidArchive("ZIP contains duplicate member names")
            total_size = sum(member.file_size for member in members)
            if total_size > settings.max_uncompressed_bytes:
                raise InvalidArchive("ZIP exceeds the uncompressed size limit")
            file_names = {member.filename for member in members if not member.is_dir()}
            if not file_names or not set(source.expected_members) <= file_names:
                raise InvalidArchive("ZIP is missing an expected file member")
            inspected: list[dict] = []
            bytes_read = 0
            for member in members:
                name = PurePosixPath(member.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in member.filename:
                    raise InvalidArchive("ZIP contains an unsafe member name")
                if member.flag_bits & 1 or stat.S_ISLNK(member.external_attr >> 16):
                    raise InvalidArchive("Encrypted or symbolic-link ZIP entries are not supported")
                if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise InvalidArchive("Only stored or deflated ZIP entries are supported")
                if member.is_dir():
                    continue
                # Read in-place to exercise decompression and CRC verification; never extract.
                with archive.open(member) as handle:
                    while chunk := handle.read(128 * 1024):
                        bytes_read += len(chunk)
                        if bytes_read > settings.max_uncompressed_bytes:
                            raise InvalidArchive("ZIP exceeds the actual uncompressed size limit")
                inspected.append(
                    {
                        "name": member.filename,
                        "compressed_bytes": member.compress_size,
                        "uncompressed_bytes": member.file_size,
                        "crc32": f"{member.CRC:08x}",
                    }
                )
            return inspected
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        raise InvalidArchive(f"ZIP could not be read or failed its CRC: {error}") from error


def validate_csv(
    path: Path,
    source: Source,
    settings: CollectorSettings,
) -> dict:
    """Check CSV structure without interpreting counts or rewriting bytes."""
    try:
        with path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.reader(handle, strict=True)
            header = next(reader, None)

            if not header:
                raise InvalidPayload("CSV is missing its header")

            normalized = [name.strip() for name in header]

            if any(not name for name in normalized) or len(set(normalized)) != len(normalized):
                raise InvalidPayload("CSV has blank or duplicate column names")

            if not set(source.expected_columns) <= set(normalized):
                raise InvalidPayload("CSV is missing required columns")

            count = 0

            for row in reader:
                if not row:
                    continue

                count += 1

                if len(row) != len(header):
                    raise InvalidPayload(f"CSV record {count} has an unexpected field count")

                if count > settings.max_csv_rows:
                    raise InvalidPayload("CSV exceeds the configured row limit")

        return {
            "columns": header,
            "normalized_columns": normalized,
            "row_count": count,
            "encoding": "utf-8-sig",
            "delimiter": ",",
        }

    except (UnicodeError, csv.Error) as error:
        raise InvalidPayload(f"CSV could not be decoded/read: {error}") from error


def validate_payload(
    path: Path,
    source: Source,
    settings: CollectorSettings,
) -> dict:
    if source.format in {"zip", "bundle"}:
        return {"zip_members": validate_archive(path, source, settings)}

    if source.format == "csv":
        return {"csv_structure": validate_csv(path, source, settings)}

    raise InvalidPayload(f"No validator implemented for {source.format}")
