"""Archive-level checks only. These do not validate election returns or vote totals."""

from __future__ import annotations

import stat
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from election_pipeline.settings import CollectorSettings, Source


class InvalidArchive(ValueError):
    pass


def validate_archive(path: Path, source: Source, settings: CollectorSettings) -> list[dict]:
    if source.format != "zip":
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
