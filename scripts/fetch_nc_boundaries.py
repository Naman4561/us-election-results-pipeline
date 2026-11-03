"""Download official display boundaries once; produce a local SVG geometry bundle."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/election_pipeline/dashboard/static/nc-map.json"
SOURCES = {
    "counties": (
        "https://gis11.services.ncdot.gov/arcgis/rest/services/NCDOT_CountyBdy_Poly/MapServer/0",
        "FIPS,CountyName",
    ),
    "districts": (
        "https://services5.arcgis.com/gRcZqepTaRC6tVZL/arcgis/rest/services/"
        "NCGA_Congress_2025/FeatureServer/0",
        "District",
    ),
}


def main():
    layers, provenance = {}, {}
    for name, (service, fields) in SOURCES.items():
        url = (
            service
            + "/query?"
            + urlencode(
                {
                    "where": "1=1",
                    "outFields": fields,
                    "outSR": 32119,
                    "maxAllowableOffset": 250,
                    "geometryPrecision": 1,
                    "returnGeometry": "true",
                    "f": "json",
                }
            )
        )
        with urlopen(
            Request(url, headers={"User-Agent": "ElectionResultsPipeline/0.1"}), timeout=60
        ) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError("Boundary response exceeds size limit")
        data = json.loads(raw)
        if "error" in data or data.get("exceededTransferLimit"):
            raise ValueError(f"Incomplete boundary response: {name}")
        features = data["features"]
        if len(features) != (100 if name == "counties" else 14):
            raise ValueError(f"Unexpected number of {name}")
        for feature in features:
            if not feature.get("geometry", {}).get("rings"):
                raise ValueError("Missing polygon geometry")
        layers[name] = features
        provenance[name] = {"url": url, "sha256": hashlib.sha256(raw).hexdigest()}

    county_ids = {int(f["attributes"]["FIPS"]) for f in layers["counties"]}
    district_ids = {int(f["attributes"]["District"]) for f in layers["districts"]}
    if county_ids != set(range(1, 200, 2)) or district_ids != set(range(1, 15)):
        raise ValueError("Boundary identifiers are incomplete or duplicated")

    points = [
        p
        for features in layers.values()
        for feature in features
        for ring in feature["geometry"]["rings"]
        for p in ring
    ]
    xmin, xmax = min(p[0] for p in points), max(p[0] for p in points)
    ymin, ymax = min(p[1] for p in points), max(p[1] for p in points)
    scale = 1160 / (xmax - xmin)
    height = round((ymax - ymin) * scale + 40, 1)
    result = {
        "viewBox": [0, 0, 1200, height],
        "plan": "NC SL 2025-95 / 2026 election",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sources": provenance,
    }
    for name, features in layers.items():
        shapes = []
        for feature in features:
            a = feature["attributes"]
            identity = (
                "".join(c for c in a["CountyName"].upper() if c.isalnum())
                if name == "counties"
                else f"NC-{int(a['District']):02}"
            )
            rings = [
                [
                    (round((p[0] - xmin) * scale + 20, 1), round((ymax - p[1]) * scale + 20, 1))
                    for p in ring
                ]
                for ring in feature["geometry"]["rings"]
            ]
            coordinates = [p for ring in rings for p in ring]
            left, right = min(p[0] for p in coordinates), max(p[0] for p in coordinates)
            top, bottom = min(p[1] for p in coordinates), max(p[1] for p in coordinates)
            path = "".join("M" + "L".join(f"{x},{y}" for x, y in ring) + "Z" for ring in rings)
            shapes.append(
                {
                    "id": identity,
                    "name": a["CountyName"] if name == "counties" else identity,
                    "path": path,
                    "center": [(left + right) / 2, (top + bottom) / 2],
                }
            )
        result[name] = sorted(shapes, key=lambda f: f["id"])
    result["version"] = hashlib.sha256(
        json.dumps(result["sources"], sort_keys=True).encode()
    ).hexdigest()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    temporary.replace(OUTPUT)
    print(
        f"Validated 100 counties and 14 districts; wrote {OUTPUT.name} "
        f"({OUTPUT.stat().st_size:,} bytes)"
    )


if __name__ == "__main__":
    main()
