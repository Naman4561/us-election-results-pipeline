"""Shared strict CSV and integer readers for state parsers and reference checks."""

import csv
import re


def records(handle, required, delimiter, limit):
    """Read by column name; reject malformed shape."""
    reader = csv.reader(handle, delimiter=delimiter, strict=True)
    header = [name.strip() for name in next(reader, [])]

    if not header or any(not name for name in header) or len(set(header)) != len(header):
        raise ValueError("Missing, blank, or duplicate column names")

    if not set(required) <= set(header):
        raise ValueError(f"Missing columns: {sorted(set(required) - set(header))}")

    count = 0

    for values in reader:
        if not values:
            continue

        count += 1

        if count > limit or len(values) != len(header):
            raise ValueError(f"Line {reader.line_num}: row limit or field-count violation")

        yield reader.line_num, dict(zip(header, (value.strip() for value in values)))

    if not count:
        raise ValueError("File contains no data records")


def integer(value, label):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{label}: expected a nonnegative integer, got {value!r}")

    return int(value)
