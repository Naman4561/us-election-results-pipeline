"""How each platform reader is set up for a source, read from the election definition.

A state whose results come from a shared platform (Tally, and in time Clarity, Enhanced
Voting and the ASP.NET ENR sites) says so in a ``[platform]`` table of its state file:
which reader, the site's identity and the contest titles the site writes. The readers
hold the format; nothing about one election or one state is written in code. A table
that names something the reader does not know, or leaves a race of the state without a
title, is refused when it is read, so a wrong definition fails on the first parse and
never as a silently skipped contest.
"""

import re
from dataclasses import dataclass

from election_pipeline import elections
from election_pipeline.states import ELECTION, state_adapter


@dataclass(frozen=True)
class Tally:
    state: str
    name: str
    fips: str  # the state's two-digit FIPS code, prefixing its county location IDs
    election_id: str
    election_name: str
    types: dict  # race -> contest type code, which names its results member
    titles: dict  # contest title as the site writes it -> race
    parties: dict  # party name as the site writes it -> stored party code

    def race(self, title):
        return self.titles.get(title)


def table(code, reader, keys, optional=()):
    """The state's ``[platform]`` table, checked to be for ``reader``: every one of ``keys`` and
    no key beyond them and ``optional``."""
    raw = ELECTION.platforms.get(code)
    if raw is None:
        raise ValueError(f"{code}: the election definition has no [platform] table")
    found = dict(raw)
    if found.pop("reader", None) != reader:
        raise ValueError(f"{code}: its [platform] table is not for the {reader} reader")
    if not set(keys) <= set(found) <= {*keys, *optional}:
        raise ValueError(f"{code}: [platform] holds {sorted(keys)}, not {sorted(found)}")
    return found


def names(raw, label, code):
    """A table of text to text, with no blank or repeated entry."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{code}: [platform.{label}] must be a table with entries")
    if not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in raw.items()):
        raise ValueError(f"{code}: [platform.{label}] holds text to text entries")
    return dict(raw)


def tally(source):
    """The Tally reader's settings for ``source``'s state."""
    code = source.state
    raw = table(code, "tally", ("fips", "election_id", "election_name", "types", "titles"))
    types, titles = names(raw["types"], "types", code), names(raw["titles"], "titles", code)
    wanted = state_adapter(code).races
    if set(types) != set(wanted) or set(titles.values()) != set(wanted):
        raise ValueError(f"{code}: [platform] types and titles must cover exactly its races")
    if len(set(titles.values())) != len(titles):
        raise ValueError(f"{code}: a race has more than one contest title")
    return Tally(
        state=code,
        name=state_adapter(code).name,
        fips=raw["fips"],
        election_id=raw["election_id"],
        election_name=raw["election_name"],
        types=types,
        titles=titles,
        parties=names(elections.parties().get("tally"), "tally parties", code),
    )


def strip_county(name):
    match = re.fullmatch(r"(.+?) County", name)
    if not match:
        raise ValueError(f"Unexpected county name {name!r}")
    return match[1]


def county_or_city(name):
    """A Virginia locality keeps its suffix: several names exist as both."""
    if not re.fullmatch(r"[A-Z][A-Za-z .'-]+ (COUNTY|CITY)", name):
        raise ValueError(f"Unexpected locality {name!r}")
    return name


# How a site's locality name becomes the stored county name; a state names its mode.
COUNTY_NAMES = {"strip_county": strip_county, "county_or_city": county_or_city, "none": None}


@dataclass(frozen=True)
class EnhancedVoting:
    state: str
    name: str
    jurisdiction: str
    election_id: str
    vote_for: tuple  # the accepted "vote for" texts; empty where the site's field is not one
    titles: dict  # contest title as the site writes it -> race
    methods: dict  # the site's method group -> stored method column
    parties: dict  # party name -> code, for a site that gives none; else empty
    skip_methods: bool  # method detail incomplete on the site: only totals are kept
    county_mode: str

    def race(self, title):
        return self.titles.get(title)

    @property
    def county(self):
        """The function turning a locality name into a county name; None for a site whose
        localities are not counties (Rhode Island's towns)."""
        return COUNTY_NAMES[self.county_mode]


def enhanced_voting(source):
    """The Enhanced Voting reader's settings for ``source``'s state."""
    code = source.state
    raw = table(
        code,
        "enhanced_voting",
        ("jurisdiction", "election_id", "county_names", "titles"),
        ("vote_for", "methods", "skip_methods", "party_names"),
    )
    titles = names(raw["titles"], "titles", code)
    wanted = state_adapter(code).races
    if set(titles.values()) != set(wanted) or len(set(titles.values())) != len(titles):
        raise ValueError(f"{code}: [platform] titles must name each of its races once")
    if raw["county_names"] not in COUNTY_NAMES:
        raise ValueError(f"{code}: unknown county_names {raw['county_names']!r}")
    methods = names(raw["methods"], "methods", code) if "methods" in raw else {}
    return EnhancedVoting(
        state=code,
        name=state_adapter(code).name,
        jurisdiction=raw["jurisdiction"],
        election_id=raw["election_id"],
        vote_for=tuple(raw.get("vote_for", ())),
        titles=titles,
        methods=methods,
        parties=names(elections.parties().get("enhanced_voting"), "enhanced_voting parties", code)
        if raw.get("party_names")
        else {},
        skip_methods=raw.get("skip_methods", False),
        county_mode=raw["county_names"],
    )
