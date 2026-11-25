"""An election's definition, read from data: its states, sources and how each is acquired.

``config/elections/<id>/`` describes one election (docs/architecture/election-definition.md):

    election.toml           id, date, the collector's limits
    states/<CODE>.toml      a state's races, vote methods, reporting policy, source and
                            acquisition: everything about it that is true of this election
    datasets/<name>.toml    a source that is not a state's results (NC's absentee file)

What is true of a state in every election (its name, its parser, the kind of place it reports
by) is ``DURABLE``. ``load`` joins the two into the ``State`` and ``Source`` objects the rest
of the code uses, so a new election is a new folder of data.

``describe`` writes a loaded election as plain data, in a fixed order; the golden snapshot
in ``tests/fixtures/registry/`` is its output for 2026, taken from the code that predates
the definition.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.states import State

ROOT = Path(__file__).resolve().parents[2] / "config/elections"
ACQUISITIONS = ("http", "manual")
# Durable per state: (name, parser module under election_pipeline.parsers, local unit).
# An empty parser module means the state's lowercase code; local_unit is "county", "town"
# (a state that reports and is drawn by town) or "house_district".
DURABLE = {
    "AK": ("Alaska", "", "house_district"),
    "AL": ("Alabama", "", "county"),
    "AR": ("Arkansas", "", "county"),
    "AZ": ("Arizona", "", "county"),
    "CA": ("California", "", "county"),
    "CO": ("Colorado", "", "county"),
    "CT": ("Connecticut", "", "town"),
    "DE": ("Delaware", "", "county"),
    "FL": ("Florida", "", "county"),
    "GA": ("Georgia", "", "county"),
    "HI": ("Hawaii", "", "county"),
    "IA": ("Iowa", "", "county"),
    "ID": ("Idaho", "", "county"),
    "IL": ("Illinois", "", "county"),
    "IN": ("Indiana", "indiana", "county"),
    "KS": ("Kansas", "", "county"),
    "KY": ("Kentucky", "", "county"),
    "LA": ("Louisiana", "", "county"),
    "MA": ("Massachusetts", "", "town"),
    "MD": ("Maryland", "", "county"),
    "ME": ("Maine", "", "county"),
    "MI": ("Michigan", "", "county"),
    "MN": ("Minnesota", "", "county"),
    "MO": ("Missouri", "", "county"),
    "MS": ("Mississippi", "", "county"),
    "MT": ("Montana", "", "county"),
    "NC": ("North Carolina", "", "county"),
    "ND": ("North Dakota", "", "county"),
    "NE": ("Nebraska", "", "county"),
    "NH": ("New Hampshire", "", "county"),
    "NJ": ("New Jersey", "", "county"),
    "NM": ("New Mexico", "", "county"),
    "NV": ("Nevada", "", "county"),
    "NY": ("New York", "", "county"),
    "OH": ("Ohio", "", "county"),
    "OK": ("Oklahoma", "", "county"),
    "OR": ("Oregon", "oregon", "county"),
    "PA": ("Pennsylvania", "", "county"),
    "RI": ("Rhode Island", "", "county"),
    "SC": ("South Carolina", "", "county"),
    "SD": ("South Dakota", "", "county"),
    "TN": ("Tennessee", "", "county"),
    "TX": ("Texas", "", "county"),
    "UT": ("Utah", "", "county"),
    "VA": ("Virginia", "", "county"),
    "VT": ("Vermont", "", "town"),
    "WA": ("Washington", "", "county"),
    "WI": ("Wisconsin", "", "county"),
    "WV": ("West Virginia", "", "county"),
    "WY": ("Wyoming", "", "county"),
}
# A state file's keys that are fields of State (the rest are races, the source and how it is
# acquired). Durable fields are not allowed in a definition.
STATE_KEYS = {
    "county_results",
    "vote_basis",
    "collection_backend",
    "county_feed_status",
    "county_privacy_masking",
    "method_note",
    "statewide_only_votes",
    "optional_house",
    "collected_counties",
}
SOURCE_KEYS = {f.name for f in fields(Source)} - {"source_id", "state", "election_date"}


@dataclass(frozen=True)
class Acquisition:
    """How a source's snapshots arrive. ``http``: the worker polls it. ``manual``: an
    operator imports an official file; ``final`` says its issuer posts nothing more, and
    ``max_age_seconds`` how old the last import may be before it is reported overdue."""

    mode: str = "http"
    final: bool = False
    max_age_seconds: int | None = None

    def __post_init__(self):
        if self.mode not in ACQUISITIONS:
            raise ValueError(f"Acquisition must be one of {ACQUISITIONS}, not {self.mode!r}")
        if self.mode == "http" and (self.final or self.max_age_seconds is not None):
            raise ValueError("Only a manual source has a final flag or a freshness limit")


@dataclass(frozen=True)
class Election:
    id: str
    date: str
    settings: CollectorSettings
    states: dict[str, State]
    sources: dict[str, Source]
    acquisition: dict[str, Acquisition]


def read(path):
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def source_of(raw, state, election_date, where):
    """The ``Source`` a definition's ``[source]`` table describes."""
    table = dict(raw)
    name = table.pop("id", None)
    table.pop("page", None)
    if not name:
        raise ValueError(f"{where}: a source needs an id")
    unknown = set(table) - SOURCE_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown source settings {sorted(unknown)}")
    for key in ("expected_members", "expected_columns", "supplemental_members"):
        table[key] = tuple(table.get(key, ()))
    table["files"] = tuple(sorted(dict(table.get("files", {})).items()))
    table["collector_overrides"] = tuple(sorted(dict(table.get("collector_overrides", {})).items()))
    table["collect_hours_utc"] = tuple(table.get("collect_hours_utc", ()))
    return Source(source_id=name, state=state, election_date=election_date, **table)


def acquisition_of(raw):
    raw = dict(raw or {})
    unknown = set(raw) - {"mode", "final", "max_age_seconds"}
    if unknown:
        raise ValueError(f"Unknown acquisition settings {sorted(unknown)}")
    return Acquisition(**raw)


def load(election="2026-general", root=None):
    """The election folder ``root/election``, as the objects the rest of the code uses."""
    folder = Path(root or ROOT) / election
    head = read(folder / "election.toml")
    if set(head) - {"id", "date", "collector"}:
        raise ValueError("election.toml holds id, date and collector only")
    if head["id"] != election:
        raise ValueError(f"{folder.name}/election.toml says it is {head['id']!r}")
    when = head["date"].isoformat() if isinstance(head["date"], date) else head["date"]
    settings = CollectorSettings(**head.get("collector", {}))
    states, sources, acquisition = {}, {}, {}
    for path in sorted((folder / "states").glob("*.toml")):
        raw = read(path)
        code = raw.pop("code", None)
        if code != path.stem or code not in DURABLE:
            raise ValueError(f"{path.name}: the file's code must be a known state's, {path.stem}")
        races, table = raw.pop("races", {}), raw.pop("source", {})
        mode = raw.pop("acquisition", {})
        if set(races) - {"senate", "governor", "house"}:
            raise ValueError(f"{path.name}: races hold senate, governor and house only")
        unknown = set(raw) - STATE_KEYS - {"methods"}
        if unknown:
            raise ValueError(f"{path.name}: unknown state settings {sorted(unknown)}")
        source = source_of(table, code, when, path.name)
        name, module, unit = DURABLE[code]
        states[code] = State(
            code,
            name,
            source.source_id,
            tuple(races.get("house", ())),
            tuple(raw["methods"]),
            senate=races.get("senate", True),
            governor=races.get("governor", False),
            source_page=table.get("page", ""),
            parser_module=module,
            local_unit=unit,
            **{k: v for k, v in raw.items() if k in STATE_KEYS},
        )
        sources[source.source_id] = source
        acquisition[source.source_id] = acquisition_of(mode)
    for path in sorted((folder / "datasets").glob("*.toml")):
        raw = read(path)
        source = source_of(raw["source"], raw["state"], when, path.name)
        sources[source.source_id] = source
        acquisition[source.source_id] = acquisition_of(raw.get("acquisition"))
    return Election(head["id"], when, settings, states, sources, acquisition)


def plain(value):
    """A dataclass as JSON-compatible data (tuples as lists), in its declared order."""
    return json.loads(json.dumps(asdict(value)))


def describe(election):
    """An election as plain data, sorted: what the golden snapshot records."""
    return {
        "id": election.id,
        "date": election.date,
        "collector": plain(election.settings),
        "states": {code: plain(s) for code, s in sorted(election.states.items())},
        "sources": {name: plain(s) for name, s in sorted(election.sources.items())},
        "acquisition": {name: plain(a) for name, a in sorted(election.acquisition.items())},
    }
