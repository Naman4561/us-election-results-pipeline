"""Build national/state SVG geometry from official Census data; never fetch vote counts.

The geometry comes from two dated TIGERweb services, not the "current" ones, so the same
request always describes the same boundaries: ``tigerWMS_ACS2026`` (January 1, 2026
vintage: states, counties, county subdivisions, the 120th Congress, 2026 lower-house
districts) and ``tigerWMS_ACS2025`` for Missouri's 119th-Congress districts (its 2025 map
is the one in force). Every response is kept by SHA-256 in the archive directory
(``map-sources`` in the repository by default) with an index of the URL each came from,
and each map records the URL and SHA-256 behind it. ``--offline`` rebuilds from the archive alone, with
no request, and produces the same files. ``--output DIR`` writes the maps elsewhere than
the dashboard's static folder, to compare before replacing (``compare_maps.py``).
"""

import argparse
import hashlib
import heapq
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from election_pipeline.states import STATES as REGISTERED

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/election_pipeline/dashboard/static"
BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
USER_AGENT = "US-Election-Results-Pipeline/0.1"
ARCHIVE = ROOT / "map-sources"
VINTAGE = "tigerWMS_ACS2026"
MISSOURI_VINTAGE = "tigerWMS_ACS2025"
# Layers of those services (see the service's MapServer?f=json).
STATE_LAYER, COUNTY_LAYER, SUBDIVISION_LAYER, CONGRESS_LAYER, LOWER_LAYER = 80, 82, 22, 54, 58
FULL_NAME_STATES = {"VA", "MD"}
# The kind of place each state's results are reported by is in its registry entry
# (``State.local_unit``): towns are Census county subdivisions (Vermont's towns and
# cities), not counties; Alaska, which has no counties, is drawn by 2026 House district.
TOWN_STATES = {code for code, state in REGISTERED.items() if state.local_unit == "town"}
HOUSE_DISTRICT_STATES = {
    code for code, state in REGISTERED.items() if state.local_unit == "house_district"
}
STATES = set(
    (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
        "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
    ).split()
)


class Archive:
    """Every response by SHA-256, and the URL each came from (``index.json``)."""

    def __init__(self, directory, offline=False):
        self.directory, self.offline = Path(directory), offline
        try:
            self.index = json.loads((self.directory / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.index = {}

    def fetch(self, url):
        if self.offline:
            digest = self.index.get(url)
            if digest is None:
                raise ValueError(f"Not in the archive (rebuild without --offline): {url}")
            data = (self.directory / f"{digest}.json").read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Archived response {digest} is corrupt")
            return data
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024:
            raise ValueError("Geography download exceeds budget")
        digest = hashlib.sha256(data).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{digest}.json").write_bytes(data)
        self.index[url] = digest
        (self.directory / "index.json").write_text(
            json.dumps(self.index, indent=1, sort_keys=True), encoding="utf-8"
        )
        return data


ARCHIVED = Archive(ARCHIVE)


def query(service, layer, where, fields, projection=5070, offset=500):
    params = {
        "where": where,
        "outFields": fields,
        "outSR": projection,
        "returnGeometry": "true",
        "maxAllowableOffset": offset,
        "geometryPrecision": 1,
        "f": "json",
    }
    url = f"{BASE}{service}/MapServer/{layer}/query?{urllib.parse.urlencode(params)}"
    data = ARCHIVED.fetch(url)
    payload = json.loads(data)
    if payload.get("error") or payload.get("exceededTransferLimit") or not payload.get("features"):
        raise ValueError(f"Incomplete geography response: {payload.get('error')}")
    return payload["features"], {"url": url, "sha256": hashlib.sha256(data).hexdigest()}


def hawaiian_main_islands(feature):
    # EPSG:3759 bounds around the inhabited islands (roughly 161W–154W, 18N–23N).
    # Omitting the uninhabited northwestern chain keeps the national inset legible.
    rings = [
        r
        for r in feature["geometry"]["rings"]
        if any(500000 < x < 3200000 and -1200000 < y < 750000 for x, y in r)
    ]
    if not rings:
        raise ValueError("Hawaii geometry lacks the main islands")
    return {**feature, "geometry": {**feature["geometry"], "rings": rings}}


def bounds(features):
    points = [
        point for feature in features for ring in feature["geometry"]["rings"] for point in ring
    ]
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def label_point(rings, precision=0.5):
    """Where a label fits best: the point inside the largest ring farthest from its edge
    (the pole of inaccessibility, found as in Mapbox's polylabel). A bounding box's centre
    can fall outside an L-shaped state: Florida's is in the Gulf."""

    def area(ring):
        return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) / 2

    ring = [tuple(p) for p in max((r for r in rings if len(r) > 2), key=lambda r: abs(area(r)))]
    edges = list(zip(ring, ring[1:] + ring[:1]))

    def distance(x, y):
        """Distance to the ring's edge: positive inside, negative outside."""
        inside, best = False, float("inf")
        for (ax, ay), (bx, by) in edges:
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                inside = not inside
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy or 1)))
            best = min(best, (ax + t * dx - x) ** 2 + (ay + t * dy - y) ** 2)
        return (1 if inside else -1) * best**0.5

    def cell(x, y, half):
        d = distance(x, y)
        return (-(d + half * 2**0.5), x, y, half, d)  # ordered by the most it can hold

    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    size = min(right - left, bottom - top)
    if size <= 0:
        return [(left + right) / 2, (top + bottom) / 2]
    heap = [
        cell(left + size * (i + 0.5), top + size * (j + 0.5), size / 2)
        for i in range(int((right - left) / size) + 1)
        for j in range(int((bottom - top) / size) + 1)
    ]
    heapq.heapify(heap)
    best = max(
        (
            cell(sum(xs) / len(xs), sum(ys) / len(ys), 0),
            cell((left + right) / 2, (top + bottom) / 2, 0),
        ),
        key=lambda c: c[4],
    )
    while heap:
        candidate = heapq.heappop(heap)
        if candidate[4] > best[4]:
            best = candidate
        if -candidate[0] - best[4] <= precision:
            continue
        half = candidate[3] / 2
        for dx in (-half, half):
            for dy in (-half, half):
                heapq.heappush(heap, cell(candidate[1] + dx, candidate[2] + dy, half))
    return [round(best[1], 1), round(best[2], 1)]


def callouts(states, room=10.5, gap=24, offset=8):
    """Give each drawn state too small to hold its label (its label point nearer than
    ``room`` to its border: Rhode Island, Delaware…) a ``callout``: a point in a column
    just past their eastmost edge, in north-to-south order at least ``gap`` apart, where
    the map writes the label at the end of a leader line."""

    def rings(path):
        return [
            [tuple(map(float, p.split(","))) for p in part[1:].rstrip("Z").split("L") if p]
            for part in re.findall(r"M[^M]+", path)
        ]

    def clearance(x, y, shape):
        best = float("inf")
        for ring in shape:
            for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
                dx, dy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy or 1)))
                best = min(best, ((ax + t * dx - x) ** 2 + (ay + t * dy - y) ** 2) ** 0.5)
        return best

    small = []
    for state in states:
        shape = rings(state["path"])
        state.pop("callout", None)
        if clearance(*state["center"], shape) < room:
            small.append((state, max(x for ring in shape for x, _ in ring)))
    if not small:
        return states
    column = max(east for _, east in small) + offset
    y = float("-inf")
    for state, _ in sorted(small, key=lambda s: s[0]["center"][1]):
        y = max(state["center"][1], y + gap)
        state["callout"] = [round(column, 1), round(y, 1)]
    return states


def layout(features, box):
    left, bottom, right, top = bounds(features)
    x, y, width, height = box
    scale = min(width / (right - left), height / (top - bottom))
    ox, oy = x + (width - (right - left) * scale) / 2, y + (height - (top - bottom) * scale) / 2

    def draw(feature):
        rings = [
            [(ox + (a - left) * scale, oy + (top - b) * scale) for a, b in ring]
            for ring in feature["geometry"]["rings"]
        ]
        path = "".join(
            "M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in ring) + "Z"
            for ring in rings
            if len(ring) > 2
        )
        return {"path": path, "center": label_point(rings)}

    return draw


def county_key(name):
    # Fold accents so Census "Doña Ana" keys like the state's "Dona Ana" (DONAANA).
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", plain.upper())


OUTPUT = STATIC


def save(name, value):
    version = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
    value["version"] = version
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / name).write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", nargs="+", default=sorted(REGISTERED))
    parser.add_argument("--output", type=Path, default=STATIC, help="Where to write the maps")
    parser.add_argument("--archive", type=Path, default=ARCHIVE, help="Where responses are kept")
    parser.add_argument(
        "--offline", action="store_true", help="Build from the archive alone; request nothing"
    )
    args = parser.parse_args()
    global ARCHIVED, OUTPUT
    ARCHIVED, OUTPUT = Archive(args.archive, args.offline), args.output
    sources = {}
    states, sources["states"] = query(VINTAGE, STATE_LAYER, "1=1", "STATE,STUSAB,NAME", offset=1500)
    states = [f for f in states if f["attributes"]["STUSAB"] in STATES]
    codes = {f["attributes"]["STATE"]: f["attributes"]["STUSAB"] for f in states}
    ids = ",".join("'" + fips + "'" for fips in sorted(codes))
    districts, sources["districts"] = query(
        VINTAGE,
        CONGRESS_LAYER,
        f"STATE IN ({ids}) AND STATE <> '29' AND CD120 NOT IN ('ZZ','98')",
        "STATE,CD120,NAME",
    )
    mo, sources["missouri_override"] = query(
        MISSOURI_VINTAGE, CONGRESS_LAYER, "STATE = '29' AND CD119 <> 'ZZ'", "STATE,CD119,NAME"
    )
    for feature in mo:
        feature["attributes"]["CD120"] = feature["attributes"].pop("CD119")
    districts += mo
    if len(states) != 50 or len(districts) != 435:
        raise ValueError(
            f"Expected 50 states and 435 voting districts; got {len(states)}, {len(districts)}"
        )
    # Regional projections keep Alaska and Hawaii legible without wrapping the dateline.
    for fips, projection in (("02", 3338), ("15", 3759)):
        regional, sources[fips + "_state"] = query(
            VINTAGE, STATE_LAYER, f"STATE = '{fips}'", "STATE,STUSAB,NAME", projection, 1500
        )
        if fips == "15":
            regional = [hawaiian_main_islands(f) for f in regional]
        states = [f for f in states if f["attributes"]["STATE"] != fips] + regional
        regional, sources[fips + "_districts"] = query(
            VINTAGE, CONGRESS_LAYER, f"STATE = '{fips}'", "STATE,CD120,NAME", projection
        )
        if fips == "15":
            regional = [hawaiian_main_islands(f) for f in regional]
        districts = [f for f in districts if f["attributes"]["STATE"] != fips] + regional
    draws = {}
    for region, box in (
        ("main", (90, 20, 1060, 600)),
        ("AK", (25, 505, 260, 185)),
        ("HI", (300, 590, 160, 100)),
    ):
        features = [
            f
            for f in states
            if (
                codes[f["attributes"]["STATE"]]
                if codes[f["attributes"]["STATE"]] in {"AK", "HI"}
                else "main"
            )
            == region
        ]
        draws[region] = layout(features, box)

    def drawn(feature):
        code = codes[feature["attributes"]["STATE"]]
        return draws[code if code in {"AK", "HI"} else "main"](feature)

    def district(feature, draw):
        a = feature["attributes"]
        code, number = codes[a["STATE"]], a["CD120"]
        identity = code + ("-AL" if number == "00" else f"-{int(number):02}")
        return {"id": identity, "state": code, "name": identity, **draw(feature)}

    national = {
        "viewBox": [0, 0, 1200, 720],
        "plan": "120th Congress / Missouri 119th override",
        "sources": sources,
        "states": callouts(
            [
                {"id": codes[f["attributes"]["STATE"]], "name": f["attributes"]["NAME"], **drawn(f)}
                for f in states
            ]
        ),
        "districts": [district(f, drawn) for f in districts],
    }
    save("us-map.json", national)
    for code in args.states:
        if code not in STATES:
            raise ValueError("Unknown state abbreviation")
        feature = next(f for f in states if codes[f["attributes"]["STATE"]] == code)
        fips = feature["attributes"]["STATE"]
        layer = (
            LOWER_LAYER
            if code in HOUSE_DISTRICT_STATES
            else SUBDIVISION_LAYER
            if code in TOWN_STATES
            else COUNTY_LAYER
        )
        counties, county_source = query(
            VINTAGE,
            layer,
            f"STATE = '{fips}'",
            "STATE,GEOID,BASENAME,NAME",
            projection={"AK": 3338, "HI": 3759}.get(code, 5070),
            offset=200,
        )
        if code == "HI":
            counties = [hawaiian_main_islands(f) for f in counties]
        draw = layout([feature], (20, 20, 1160, 640))
        # Virginia's independent cities and Baltimore City share base names with counties
        # (Fairfax County / Fairfax city), so those states key counties by full name.
        label = "NAME" if code in FULL_NAME_STATES else "BASENAME"
        if code in TOWN_STATES:
            # Census marks open water (Connecticut's Long Island Sound) as a subdivision
            # "not defined"; no one votes there.
            counties = [
                f
                for f in counties
                if f["attributes"]["BASENAME"] != "County subdivisions not defined"
            ]
            if code == "MA":
                # Census names Massachusetts towns with a city form of government
                # "Agawam Town"; the state and its results call them "Agawam".
                for f in counties:
                    a = f["attributes"]
                    a["BASENAME"] = re.sub(r" Town$", "", a["BASENAME"])
            # A city and a town may share a base name (Barre city, Barre town): those
            # keep their full Census name; every other town keeps its base name.
            shared = Counter(county_key(f["attributes"]["BASENAME"]) for f in counties)
            for f in counties:
                a = f["attributes"]
                a["LABEL"] = a["NAME"] if shared[county_key(a["BASENAME"])] > 1 else a["BASENAME"]
            label = "LABEL"
        if code in HOUSE_DISTRICT_STATES:
            for f in counties:
                f["attributes"]["LABEL"] = f"House District {int(f['attributes']['BASENAME'])}"
            label = "LABEL"
        local_districts = [f for f in districts if f["attributes"]["STATE"] == fips]
        save(
            code.lower() + "-map.json",
            {
                "viewBox": [0, 0, 1200, 680],
                "plan": national["plan"],
                "state": code,
                "outline": {"id": code, "name": feature["attributes"]["NAME"], **draw(feature)},
                "sources": {"counties": county_source, "districts": sources["districts"]},
                "counties": [
                    {
                        "id": county_key(f["attributes"][label]),
                        "name": f["attributes"][label],
                        "fips": f["attributes"]["GEOID"],
                        **draw(f),
                    }
                    for f in counties
                ],
                "districts": [district(f, draw) for f in local_districts],
            },
        )
    print("Generated national districts and state county maps:", ", ".join(args.states))


if __name__ == "__main__":
    main()
