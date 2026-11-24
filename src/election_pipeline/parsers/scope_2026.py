"""Reviewed official candidate/county rosters for the 2026 general election.

Each state's roster is a reviewed data file, ``resources/rosters_2026/<STATE>.json``,
recording which official capture it was taken from. The files are part of the parser
code hash (``pipeline.collector_code_hash``), so changing a roster forces a reparse.
``scripts/review_rosters.py`` prints the roster a captured snapshot implies, for review.

A state's file is read the first time that state's roster is asked for, so a parse reads
its own state's roster alone: another state's roster can neither change nor break it,
and a release check keys each roster file to its own state (``deployment.control``).
"""

import json
import re
from collections.abc import Mapping
from importlib.resources import files


class Rosters(Mapping):
    """{state: {race: roster}}, each state's file loaded on first use."""

    def __init__(self, folder=None):
        self._folder = folder or files("election_pipeline").joinpath("resources/rosters_2026")
        self._loaded = {}

    def __getitem__(self, code):
        if code not in self._loaded:
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code):
                raise KeyError(code)
            entry = self._folder.joinpath(f"{code}.json")
            if not entry.is_file():
                raise KeyError(code)
            document = json.loads(entry.read_text(encoding="utf-8"))
            if document["state"] != code:
                raise ValueError(f"Roster file {entry.name} does not match its state")
            self._loaded[code] = document["races"]
        return self._loaded[code]

    def __iter__(self):
        names = sorted(e.name for e in self._folder.iterdir() if e.name.endswith(".json"))
        return iter(name.removesuffix(".json") for name in names)

    def __len__(self):
        return sum(1 for _ in self)


ROSTERS = Rosters()
