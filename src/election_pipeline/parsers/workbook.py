"""Bounded, read-only XLSX values reader. Formula cells are never evaluated."""

import io
import re
import zipfile
from pathlib import PurePosixPath

from election_pipeline.parsers.structured import xml


class Workbook:
    def __init__(self, raw, settings):
        self.archive = zipfile.ZipFile(io.BytesIO(raw))
        self.settings = settings
        entries = self.archive.infolist()
        names = [e.filename for e in entries]
        if (
            len(entries) > 500
            or len(names) != len(set(names))
            or sum(e.file_size for e in entries) > settings.max_uncompressed_bytes
        ):
            raise ValueError("Workbook exceeds its entry/uncompressed budget or duplicates names")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or entry.flag_bits & 1
            ):
                raise ValueError("Unsafe workbook entry")
        self.shared = []
        if "xl/sharedStrings.xml" in names:
            root = xml(self.archive.read("xl/sharedStrings.xml"))
            self.shared = [
                "".join(n.text or "" for n in item.iter("t")) for item in root.findall("si")
            ]
        relations = xml(self.archive.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for relation in relations:
            if relation.get("TargetMode") == "External":
                raise ValueError("External workbook relationships are unsupported")
            identity = relation.get("Id")
            if identity in targets:
                raise ValueError("Duplicate workbook relationship")
            target = relation.get("Target", "")
            targets[identity] = target.lstrip("/") if target.startswith("/xl/") else "xl/" + target
        book = xml(self.archive.read("xl/workbook.xml"))
        self.sheets = {}
        for sheet in book.findall("sheets/sheet"):
            name = sheet.get("name")
            if name in self.sheets:
                raise ValueError("Duplicate workbook sheet")
            identity = sheet.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            self.sheets[name] = targets[identity]

    def close(self):
        self.archive.close()

    def rows(self, sheet):
        root = xml(self.archive.read(self.sheets[sheet]))
        rows = []
        previous_row = 0
        for node in root.findall("sheetData/row"):
            number = int(node.get("r"))
            if number <= previous_row:
                raise ValueError("Unordered/excessive workbook rows")
            previous_row = number
            # Formatted but empty rows past the data hold no values and count for nothing.
            if all(
                cell.find("v") is None and cell.get("t") != "inlineStr"
                for cell in node.findall("c")
            ):
                continue
            if number > min(self.settings.max_csv_rows, 1000):
                raise ValueError("Unordered/excessive workbook rows")
            values = {}
            for cell in node.findall("c"):
                match = re.fullmatch(r"([A-Z]{1,3})([1-9]\d*)", cell.get("r", ""))
                if not match or int(match[2]) != number or cell.find("f") is not None:
                    raise ValueError("Invalid workbook cell address or formula")
                column = 0
                for character in match[1]:
                    column = column * 26 + ord(character) - ord("A") + 1
                if column > 512 or column in values:
                    raise ValueError("Duplicate/excessive workbook column")
                value = cell.findtext("v")
                kind = cell.get("t")
                if kind == "s":
                    if value is None or not value.isdecimal() or int(value) >= len(self.shared):
                        raise ValueError("Invalid workbook shared-string index")
                    value = self.shared[int(value)]
                elif kind == "inlineStr":
                    value = "".join(n.text or "" for n in cell.findall("is/t"))
                elif kind not in (None, "n"):
                    raise ValueError("Unsupported workbook cell type")
                elif value is not None:
                    if not re.fullmatch(r"[0-9]+", value):
                        raise ValueError("Expected an integer workbook number")
                    value = int(value)
                values[column] = value
            while len(rows) < number:
                rows.append([])
            rows[number - 1] = [values.get(i) for i in range(1, max(values, default=0) + 1)]
        return rows
