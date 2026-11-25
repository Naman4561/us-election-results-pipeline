"""An election's definition, read from data: its states, sources and how each is acquired.

``config/elections/<id>/`` describes one election (docs/architecture/election-definition.md):

    election.toml           id, date, the collector's limits
    states/<CODE>.toml      a state's races, vote methods, reporting policy, source and
                            acquisition: everything about it that is true of this election
    datasets/<name>.toml    a source that is not a state's results (NC's absentee file)

What is true of a state in every election (its name, its parser, the kind of place it reports
by) is ``config/states.toml``; ``config/elections/current`` names the election in force.
``load`` joins them into the ``State`` and ``Source`` objects the rest of the code uses, so a
new election is a new folder of data and a new line in ``current``.

``describe`` writes a loaded election as plain data, in a fixed order; the golden snapshot
in ``tests/fixtures/registry/`` is its output for 2026, taken from the code that predates
the definition.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from election_pipeline.settings import CollectorSettings, Source
from election_pipeline.state_model import State

CONFIG = Path(__file__).resolve().parents[2] / "config"
ROOT = CONFIG / "elections"
ACQUISITIONS = ("http", "manual")
DURABLE_KEYS = {"name", "local_unit", "parser_module"}
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


def current(config=None):
    """The id of the election in force, from ``elections/current`` in the config folder."""
    return (Path(config or CONFIG) / "elections/current").read_text(encoding="utf-8").strip()


def folder(config=None):
    """The folder of the election in force."""
    return Path(config or CONFIG) / "elections" / current(config)


def durable(config=None):
    """{code: its durable facts}: what is true of a state in every election."""
    found = read(Path(config or CONFIG) / "states.toml")
    for code, entry in found.items():
        unknown = set(entry) - DURABLE_KEYS
        if unknown or not {"name", "local_unit"} <= set(entry):
            raise ValueError(
                f"states.toml {code}: a name, a local unit and nothing but {sorted(DURABLE_KEYS)}"
            )
    return found


def fingerprint(code, election=None, config=None):
    """Everything the definition says about state ``code``: its durable facts and its file in
    the election, which is what a check or publication of that state depends on beyond code.
    Changing another state's does not change it."""
    config = Path(config or CONFIG)
    path = Path(election or folder(config)) / "states" / f"{code}.toml"
    text = path.read_bytes().replace(b"\r\n", b"\n")
    facts = json.dumps(durable(config)[code], sort_keys=True).encode()
    return hashlib.sha256(text + b"\0" + facts).hexdigest()


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


def load(election=None, root=None, config=None):
    """The election ``root/election`` (default: the one in force), as the objects the rest of
    the code uses. ``config`` is the folder holding ``states.toml``."""
    config = Path(config or CONFIG)
    election = election or current(config)
    folder = Path(root or config / "elections") / election
    facts = durable(config)
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
        if code != path.stem or code not in facts:
            raise ValueError(f"{path.name}: the file's code must be a known state's, {path.stem}")
        races, table = raw.pop("races", {}), raw.pop("source", {})
        mode = raw.pop("acquisition", {})
        if set(races) - {"senate", "governor", "house"}:
            raise ValueError(f"{path.name}: races hold senate, governor and house only")
        unknown = set(raw) - STATE_KEYS - {"methods"}
        if unknown:
            raise ValueError(f"{path.name}: unknown state settings {sorted(unknown)}")
        source = source_of(table, code, when, path.name)
        states[code] = State(
            code=code,
            name=facts[code]["name"],
            source_id=source.source_id,
            house=tuple(races.get("house", ())),
            methods=tuple(raw["methods"]),
            senate=races.get("senate", True),
            governor=races.get("governor", False),
            source_page=table.get("page", ""),
            parser_module=facts[code].get("parser_module", ""),
            local_unit=facts[code]["local_unit"],
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
