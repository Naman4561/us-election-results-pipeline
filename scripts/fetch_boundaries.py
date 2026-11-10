"""Build national/state SVG geometry from official Census data; never fetch vote counts."""

import argparse
import hashlib
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
FULL_NAME_STATES = {"VA", "MD"}
# States whose results are reported by town: their map's local units are Census county
# subdivisions (Vermont's towns and cities), not counties.
TOWN_STATES = {"VT"}
STATES = set(
    (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
        "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
    ).split()
)


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
    request = urllib.request.Request(
        url, headers={"User-Agent": "US-Election-Results-Pipeline/maps"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(50 * 1024 * 1024 + 1)
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("Geography download exceeds budget")
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
        points = [p for ring in rings for p in ring]
        return {
            "path": path,
            "center": [
                (min(p[0] for p in points) + max(p[0] for p in points)) / 2,
                (min(p[1] for p in points) + max(p[1] for p in points)) / 2,
            ],
        }

    return draw


def county_key(name):
    # Fold accents so Census "Doña Ana" keys like the state's "Dona Ana" (DONAANA).
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", plain.upper())


def save(name, value):
    version = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
    value["version"] = version
    STATIC.mkdir(parents=True, exist_ok=True)
    (STATIC / name).write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", nargs="+", default=sorted(REGISTERED))
    args = parser.parse_args()
    sources = {}
    states, sources["states"] = query("State_County", 0, "1=1", "STATE,STUSAB,NAME", offset=1500)
    states = [f for f in states if f["attributes"]["STUSAB"] in STATES]
    codes = {f["attributes"]["STATE"]: f["attributes"]["STUSAB"] for f in states}
    ids = ",".join("'" + fips + "'" for fips in sorted(codes))
    districts, sources["districts"] = query(
        "Legislative",
        0,
        f"STATE IN ({ids}) AND STATE <> '29' AND CD120 NOT IN ('ZZ','98')",
        "STATE,CD120,NAME",
    )
    mo, sources["missouri_override"] = query(
        "Legislative", 4, "STATE = '29' AND CD119 <> 'ZZ'", "STATE,CD119,NAME"
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
            "State_County", 0, f"STATE = '{fips}'", "STATE,STUSAB,NAME", projection, 1500
        )
        if fips == "15":
            regional = [hawaiian_main_islands(f) for f in regional]
        states = [f for f in states if f["attributes"]["STATE"] != fips] + regional
        regional, sources[fips + "_districts"] = query(
            "Legislative", 0, f"STATE = '{fips}'", "STATE,CD120,NAME", projection
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
        "states": [
            {"id": codes[f["attributes"]["STATE"]], "name": f["attributes"]["NAME"], **drawn(f)}
            for f in states
        ],
        "districts": [district(f, drawn) for f in districts],
    }
    save("us-map.json", national)
    for code in args.states:
        if code not in STATES:
            raise ValueError("Unknown state abbreviation")
        feature = next(f for f in states if codes[f["attributes"]["STATE"]] == code)
        fips = feature["attributes"]["STATE"]
        counties, county_source = query(
            "Places_CouSub_ConCity_SubMCD" if code in TOWN_STATES else "State_County",
            1,
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
            # A city and a town may share a base name (Barre city, Barre town): those
            # keep their full Census name; every other town keeps its base name.
            shared = Counter(county_key(f["attributes"]["BASENAME"]) for f in counties)
            for f in counties:
                a = f["attributes"]
                a["LABEL"] = a["NAME"] if shared[county_key(a["BASENAME"])] > 1 else a["BASENAME"]
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
