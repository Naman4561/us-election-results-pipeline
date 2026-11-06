"""Reviewed official candidate/county rosters for the 2026 general election.

Each state's roster is a reviewed data file, ``resources/rosters_2026/<STATE>.json``,
recording which official capture it was taken from. The files are part of the parser
code hash (``pipeline.collector_code_hash``), so changing a roster forces a reparse.
``scripts/review_rosters.py`` prints the roster a captured snapshot implies, for review.
"""

import json
from importlib.resources import files


def load():
    rosters = {}
    folder = files("election_pipeline").joinpath("resources/rosters_2026")
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".json"):
            continue
        document = json.loads(entry.read_text(encoding="utf-8"))
        code = document["state"]
        if entry.name != f"{code}.json" or code in rosters:
            raise ValueError(f"Roster file {entry.name} does not match its state")
        rosters[code] = document["races"]
    return rosters


ROSTERS = load()
