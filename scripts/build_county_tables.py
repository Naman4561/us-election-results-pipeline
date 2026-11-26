"""Write (or check) each state's county table from the pinned map and the reviewed roster.

``config/elections/<election>/counties/<XX>.json`` lists every county, town or house district a
state's results name: its FIPS code (from the dashboard's map, which is built from the pinned
Census files in ``map-sources/``), its display name and every spelling the reviewed roster uses
for it. Nothing here is typed by hand: change the map or the roster and run this again.

    python scripts/build_county_tables.py           # write the tables
    python scripts/build_county_tables.py --check   # fail if a table is not what this writes
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from election_pipeline import elections  # noqa: E402
from election_pipeline.counties import key  # noqa: E402
from election_pipeline.states import STATES  # noqa: E402

STATIC = ROOT / "src/election_pipeline/dashboard/static"


def roster_names(folder, code):
    path = folder / "rosters" / f"{code}.json"
    if not path.is_file():
        return set()
    races = json.loads(path.read_text(encoding="utf-8"))["races"]
    return {name for race in races.values() for name in race.get("counties", [])}


def build(folder, code):
    """The county table of state ``code``: one entry per map feature."""
    state = STATES[code]
    map_file = STATIC / f"{code.lower()}-map.json"
    features = json.loads(map_file.read_text(encoding="utf-8"))["counties"]
    by_key = {}
    for feature in features:
        if by_key.setdefault(feature["id"], feature) is not feature:
            raise SystemExit(f"{code}: two map features share the key {feature['id']}")
    spellings = {feature["id"]: {feature["name"]} for feature in features}
    for name in roster_names(folder, code):
        # The map's id is the dashboard's key for the feature: how the old join matched.
        if key(name) not in by_key:
            raise SystemExit(f"{code}: roster name {name!r} matches no map feature")
        spellings[key(name)].add(name)
    places = [
        {
            "fips": feature["fips"],
            "key": feature["id"],
            "name": feature["name"],
            "spellings": sorted(spellings[feature["id"]]),
        }
        for feature in sorted(features, key=lambda f: f["fips"])
    ]
    return {
        "state": code,
        "unit": state.local_unit,
        "provenance": (
            f"FIPS and names from the pinned Census map (dashboard/static/{code.lower()}-map.json, "
            f"built from map-sources/); spellings from rosters/{code}.json"
        ),
        "counties": places,
    }


def text(record):
    return json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def states_with_places():
    """The states whose results name counties, towns or districts: not 'county_mode none'."""
    return sorted(code for code, state in STATES.items() if state.county_mode != "none")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    folder = elections.folder()
    target = folder / "counties"
    wrong = []
    for code in states_with_places():
        wanted = text(build(folder, code))
        path = target / f"{code}.json"
        if args.check:
            if (
                not path.is_file()
                or path.read_bytes().decode("utf-8").replace("\r\n", "\n") != wanted
            ):
                wrong.append(code)
        else:
            target.mkdir(exist_ok=True)
            path.write_bytes(wanted.encode("utf-8"))
    if args.check:
        if wrong:
            raise SystemExit(f"County tables differ from the maps and rosters: {wrong}")
        print(f"{len(states_with_places())} county tables are current")
    else:
        print(f"wrote {len(states_with_places())} county tables to {target}")


if __name__ == "__main__":
    main()
