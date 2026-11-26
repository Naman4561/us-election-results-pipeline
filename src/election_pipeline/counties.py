"""Each state's counties (or towns, or house districts): FIPS and the spellings its results use.

A county table (``config/elections/<election>/counties/<XX>.json``) lists every place a state's
results can name, with its FIPS code and every spelling the election's sources write for it.
``resolve`` turns the name a parser read into the FIPS, so the database and the dashboard join on
a code rather than on text: a name that is not in the table is refused where it is parsed.

Matching is exact against the listed spellings first. Failing that it falls back to the
dashboard's old rule, the name with everything but letters and digits dropped, and only when that
names exactly one place. ``scripts/build_county_tables.py`` writes the tables from the pinned
maps and the reviewed rosters; ``tests/test_counties.py`` checks they are what it would write.
"""

import json
import re
from functools import lru_cache

from election_pipeline import elections


def key(name):
    """A name's comparison key: upper case, letters and digits only (the dashboard's old join)."""
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


class CountyTable:
    def __init__(self, record):
        self.state = record["state"]
        self.unit = record["unit"]
        self.places = {p["fips"]: p for p in record["counties"]}
        self.exact = {}
        self.keyed = {}
        for place in record["counties"]:
            for spelling in place["spellings"]:
                if self.exact.setdefault(spelling, place["fips"]) != place["fips"]:
                    raise ValueError(f"{self.state}: {spelling!r} names two places")
            self.keyed.setdefault(place["key"], set()).add(place["fips"])
        if len(self.places) != len(record["counties"]):
            raise ValueError(f"{self.state}: a FIPS code is listed twice")

    def resolve(self, name):
        """The FIPS of the place ``name`` spells; ValueError for a name the table lacks."""
        if name in self.exact:
            return self.exact[name]
        found = self.keyed.get(key(name), ())
        if len(found) == 1:
            return next(iter(found))
        kind = "ambiguous" if found else "unknown"
        raise ValueError(f"{self.state}: {kind} {self.unit} {name!r}")


@lru_cache
def table(code, folder=None):
    """The county table of state ``code`` in the election in force (or in ``folder``)."""
    where = (folder or elections.folder()) / "counties" / f"{code}.json"
    if not where.is_file():
        raise ValueError(f"{code}: the election has no county table")
    return CountyTable(json.loads(where.read_text(encoding="utf-8")))


def resolve(code, name):
    return table(code).resolve(name)
