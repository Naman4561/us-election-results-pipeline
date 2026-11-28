"""One state's registry entry: what a results source, parser and dashboard need to know.

The facts true of a state in every election (its name, parser module, kind of place) are in
``config/states.toml``; the rest are the election definition's (``config/elections/<id>/``,
see ``election_pipeline.elections``), which builds these entries.
"""

import re
from dataclasses import dataclass
from datetime import date
from importlib import import_module

LOCAL_UNITS = ("county", "town", "house_district")
# A race held beside a regular one (a special election, a runoff) names its seat and a
# suffix: SEN-S is a special Senate race, 05-R a runoff of district 5, which the registry
# and the schema list next to SEN and 05.
SPECIAL = re.compile(r"(SEN|GOV|AL|[0-9]{2})-[A-Z0-9]{1,4}")


@dataclass(frozen=True, kw_only=True)
class State:
    """A state's entry. Fields are keyword-only and checked as the entry is built, so a
    mistake in a definition fails where it is written, not where it is first used."""

    code: str
    name: str
    source_id: str
    house: tuple[int, ...]
    methods: tuple[str, ...]
    senate: bool = True
    governor: bool = False
    county_results: bool = True
    source_page: str = ""
    vote_basis: str = "reported_votes"
    collection_backend: str = ""
    county_feed_status: bool = False
    # The state hides small county counts for voter privacy (printed as "*"). Its county
    # rows may then add up to less than an official total, never more.
    county_privacy_masking: bool = False
    # Parser module under election_pipeline.parsers; defaults to the slug. Oregon's
    # slug "or" is a Python keyword, so its parser module has a different name.
    parser_module: str = ""
    # Shown under a race when the state's vote-method breakdown needs explaining.
    method_note: str = ""
    # What the state's local results units are: "county", "town" where a state reports by
    # town and its map draws towns (Vermont), or "house_district" (Alaska, which has no
    # counties).
    local_unit: str = "county"
    # Votes the state reports statewide but in no local unit (Alaska's overseas federal
    # absentee ballots), described for the dashboard. Its local units then add up to
    # less than the statewide total, never more; the parser checks the remainder.
    statewide_only_votes: str = ""
    # The House races are published only once an operator turns them on (Mississippi's
    # come from certified county recaps); until then a release has the other races only.
    optional_house: bool = False
    # Results come county by county, from each county's own report, before the state
    # posts any (Wisconsin): a release has the counties collected so far, and its
    # statewide votes are theirs alone. A House district is published once one of its
    # counties is in, and is complete once every county in it is.
    collected_counties: bool = False
    # Races held in addition to the regular ones, as (suffix, name, tab): a special Senate
    # election beside the regular seat is ("SEN-S", "U.S. Senate (special)", "Special"), contest
    # ID GA-SEN-S, and a runoff of House district 5 is ("05-R", ..., "Runoff"). The dashboard
    # shows a seat with such races as tabs, each labelled with its tab.
    specials: tuple[tuple[str, str, str], ...] = ()
    # Races whose election-day count goes to a runoff not yet held, as (suffix, date): the
    # dashboard marks the race "Runoff pending". ("05", "2026-12-12") is LA-05.
    runoffs: tuple[tuple[str, str], ...] = ()
    # House seats nobody opposed. The state leaves them off the ballot and out of its results,
    # so they have no vote counts and are no part of ``races``; they are listed on the page
    # as seats, as (suffix, winner, party, basis, source): ("10", "A B", "DEM", why, url).
    uncontested: tuple[tuple[str, str, str, str, str], ...] = ()

    def __post_init__(self):
        def refuse(reason):
            raise ValueError(f"State {self.code}: {reason}")

        if not (len(self.code) == 2 and self.code.isalpha() and self.code.isupper()):
            raise ValueError(f"State code {self.code!r} is not two capital letters")
        if not self.name:
            refuse("it needs a name")
        if not self.source_id.startswith(self.slug + "_"):
            refuse(f"its source {self.source_id!r} must start with {self.slug}_")
        if not self.methods or self.methods[0] != "total_votes":
            refuse("its first vote method must be total_votes (everything reads methods[1:])")
        if len(set(self.methods)) != len(self.methods):
            refuse("a vote method is listed twice")
        if list(self.house) != sorted(set(self.house)) or any(n < 0 for n in self.house):
            refuse("its House districts must be sorted, unique numbers (0 is at large)")
        for suffix, label, tab in self.specials:
            if not SPECIAL.fullmatch(suffix) or not label or not tab:
                refuse(f"a special race is a suffix like SEN-S, a name and a tab, not {suffix!r}")
        if len({suffix for suffix, _, _ in self.specials}) != len(self.specials):
            refuse("a special race is listed twice")
        for suffix, day in self.runoffs:
            if f"{self.code}-{suffix}" not in self.races:
                refuse(f"its runoff {suffix!r} is not one of its races")
            try:
                date.fromisoformat(day)
            except ValueError:
                refuse(f"its runoff {suffix!r} needs an ISO date, not {day!r}")
        for suffix, winner, party, basis, source in self.uncontested:
            if not re.fullmatch(r"AL|[0-9]{2}", suffix):
                refuse(f"an uncontested seat is a House district like 10 or AL, not {suffix!r}")
            if (0 if suffix == "AL" else int(suffix)) in self.house:
                refuse(f"uncontested seat {suffix} is also one of its contested House races")
            if not (winner and basis and re.fullmatch(r"[A-Z]{3}", party)):
                refuse(f"uncontested seat {suffix} needs a winner, a party code and a basis")
            if not source.startswith("https://"):
                refuse(f"uncontested seat {suffix} needs the https address of its source")
        if len({s for s, *_ in self.uncontested}) != len(self.uncontested):
            refuse("an uncontested seat is listed twice")
        if not (self.senate or self.governor or self.house or self.specials):
            refuse("it elects no Senator, Governor or Representative")
        if self.local_unit not in LOCAL_UNITS:
            refuse(f"its local unit must be one of {LOCAL_UNITS}")
        if self.optional_house and self.collected_counties:
            refuse("its House is either optional or built from collected counties, not both")
        if (self.optional_house or self.collected_counties) and not self.house:
            refuse("it has no House races to be optional or collected")
        if self.county_feed_status and self.collection_backend != "county_files":
            refuse("county feed status belongs to the county_files collection backend")

    @property
    def county_mode(self):
        """How dbt reconciles the state's county rows with its statewide totals: ``none`` (no
        county results), ``masked`` (county rows may add up to less than the total, never
        more: privacy masking or votes in no local unit) or ``exact``. The parser's own
        ``county_reconciliation`` outcome must be consistent with it (tests/test_every_source)."""
        if not self.county_results:
            return "none"
        if self.county_privacy_masking or self.statewide_only_votes:
            return "masked"
        return "exact"

    @property
    def slug(self):
        """Lowercase code naming the state's parser module and map file."""
        return self.code.lower()

    @property
    def races(self):
        return {
            **({f"{self.code}-SEN": "U.S. Senate"} if self.senate else {}),
            **({f"{self.code}-GOV": "Governor"} if self.governor else {}),
            **{f"{self.code}-{suffix}": label for suffix, label, _ in self.specials},
            **{
                (
                    identity := self.code + ("-AL" if n == 0 else f"-{n:02}")
                ): f"U.S. House · {identity}"
                for n in self.house
            },
        }

    @property
    def base_races(self):
        """The races a release must have; the House is left out where it is optional."""
        if not (self.optional_house or self.collected_counties):
            return self.races
        house = {self.code + ("-AL" if n == 0 else f"-{n:02}") for n in self.house}
        return {k: v for k, v in self.races.items() if k not in house}

    @property
    def runoff_dates(self):
        """{race ID: the date of its pending runoff}."""
        return {f"{self.code}-{suffix}": day for suffix, day in self.runoffs}

    @property
    def uncontested_races(self):
        """{race ID: its winner, party, basis and source} for each seat nobody opposed."""
        return {
            f"{self.code}-{suffix}": {
                "winner": winner,
                "party": party,
                "basis": basis,
                "source": source,
            }
            for suffix, winner, party, basis, source in self.uncontested
        }

    @property
    def variants(self):
        """{race ID: (its seat's regular race ID, its tab)} for every race of a seat that has a
        special or runoff race beside its regular one, the regular race included. A seat with
        one race is not here. The regular race's tab is "General" beside a runoff, else
        "Regular"."""
        found = {}
        for suffix, _, tab in self.specials:
            seat = f"{self.code}-{suffix.split('-')[0]}"
            found[f"{self.code}-{suffix}"] = (seat, tab)
        runoff_seats = {seat for seat, tab in found.values() if tab == "Runoff"}
        for seat in {seat for seat, _ in found.values()}:
            found[seat] = (seat, "General" if seat in runoff_seats else "Regular")
        return found

    def accepts(self, contests):
        """A release's contests are every race, or (House optional) every base race, or
        (collected counties) every base race and the House districts collected so far."""
        if self.collected_counties:
            return set(self.base_races) <= set(contests) <= set(self.races)
        return set(contests) in (set(self.races), set(self.base_races))

    @property
    def parser(self):
        return import_module(f"election_pipeline.parsers.{self.parser_module or self.slug}")

    @property
    def datasets(self):
        """Election results, plus any ancillary dataset the parser stores itself."""
        return ("election_results", *getattr(self.parser, "DATASETS", {}))

    @property
    def ranked_choice(self):
        """The races whose ranked-choice rounds the parser publishes (its ``RCV`` table)."""
        return tuple(sorted(getattr(self.parser, "RCV", {})))
