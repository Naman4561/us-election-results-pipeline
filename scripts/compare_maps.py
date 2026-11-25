"""Compare two folders of dashboard maps, state by state, before one replaces the other.

    python scripts/compare_maps.py NEW_DIR [OLD_DIR]     # OLD_DIR: the dashboard's maps

For each ``*-map.json`` it reports the drawn units (counties or towns, and districts) that
were added, removed or renamed, how many have a different shape, and by how far the
biggest change moves a unit's outline and its label point, in map units (the maps are
1200 wide). Units are matched by
``id``. A new vintage redraws every outline a little, so the shift is what says whether
a change is a redraw or a different place.
"""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/election_pipeline/dashboard/static"
POINT = re.compile(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")


def box(path):
    points = [(float(x), float(y)) for x, y in POINT.findall(path)]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def shift(old, new):
    """How far a unit's outline (its bounding box) and its label point moved, in map units."""
    a, b = box(old["path"]), box(new["path"])
    outline = max((abs(p - q) for p, q in zip(a or (), b or ())), default=0.0)
    label = max(
        (abs(p - q) for p, q in zip(old.get("center", ()), new.get("center", ()))), default=0.0
    )
    return outline, label


def compare_units(old, new):
    old_by, new_by = {u["id"]: u for u in old}, {u["id"]: u for u in new}
    both = sorted(set(old_by) & set(new_by))
    changed = [i for i in both if old_by[i]["path"] != new_by[i]["path"]]
    return {
        "old": len(old_by),
        "new": len(new_by),
        "added": sorted(set(new_by) - set(old_by)),
        "removed": sorted(set(old_by) - set(new_by)),
        "renamed": [
            (i, old_by[i]["name"], new_by[i]["name"])
            for i in both
            if old_by[i]["name"] != new_by[i]["name"]
        ],
        "reshaped": len(changed),
        "max_shift": round(max((shift(old_by[i], new_by[i])[0] for i in changed), default=0.0), 1),
        "max_label_shift": round(
            max((shift(old_by[i], new_by[i])[1] for i in changed), default=0.0), 1
        ),
    }


def compare(old_dir, new_dir):
    results = {}
    for new_path in sorted(Path(new_dir).glob("*-map.json")):
        old_path = Path(old_dir) / new_path.name
        new = json.loads(new_path.read_text(encoding="utf-8"))
        if not old_path.exists():
            results[new_path.name] = {"new_file": True}
            continue
        old = json.loads(old_path.read_text(encoding="utf-8"))
        if new_path.name == "us-map.json":
            parts = {"states": "states", "districts": "districts"}
        else:
            parts = {"units": "counties", "districts": "districts"}
        results[new_path.name] = {
            label: compare_units(old.get(key, []), new.get(key, [])) for label, key in parts.items()
        }
        results[new_path.name]["plan"] = (old.get("plan"), new.get("plan"))
    return results


def line(name, label, c):
    flags = []
    if c["added"]:
        flags.append("added " + ",".join(c["added"][:6]) + ("..." if len(c["added"]) > 6 else ""))
    if c["removed"]:
        flags.append(
            "removed " + ",".join(c["removed"][:6]) + ("..." if len(c["removed"]) > 6 else "")
        )
    if c["renamed"]:
        flags.append(f"{len(c['renamed'])} renamed")
    return (
        f"{name.removesuffix('-map.json').upper():<4}{label:<10}{c['old']:>5} ->{c['new']:>5}"
        f"  reshaped {c['reshaped']:>4}  outline shift {c['max_shift']:>6}"
        f"  label shift {c['max_label_shift']:>6}  " + "; ".join(flags)
    ).rstrip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new", type=Path)
    parser.add_argument("old", type=Path, nargs="?", default=STATIC)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = compare(args.old, args.new)
    if args.json:
        print(json.dumps(results, indent=1))
        return
    for name, parts in results.items():
        if parts.get("new_file"):
            print(f"{name}: new file")
            continue
        for label, c in parts.items():
            if label != "plan":
                print(line(name, label, c))
        if parts["plan"][0] != parts["plan"][1]:
            print(f"{name}: plan {parts['plan'][0]!r} -> {parts['plan'][1]!r}")


if __name__ == "__main__":
    main()
