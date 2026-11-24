"""Assemble New Jersey's county results into one zip for a manual import.

New Jersey publishes no statewide results before certification: each of its 21 county
clerks posts its own. This operator tool downloads, as the pipeline requests any page
(its own user agent; nothing solved or retried around a refusal), what answers: each
Clarity county's current ``summary.json`` and ``electionsettings.json``, and the
results files of the counties listed in ``FILES``. It adds, unchanged, the files the
operator saved by hand from sites that refuse automated clients (``--saved
county=path``), and writes ``results.zip`` with a ``sources.json`` naming every
member's address (or "saved by the operator"), its SHA-256 and when it was taken. The
zip is then imported as the ``nj_2026_general`` manual source (``parsers/nj.py``).

    python scripts/nj_bundle.py --out results.zip --saved bergen=bergen.pdf \\
        --saved hunterdon=hunterdon.pdf --saved warren=warren.pdf
"""

import argparse
import gzip
import hashlib
import json
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline.settings import load_config

ROOT = Path(__file__).resolve().parents[1]
CLARITY = "https://results.enr.clarityelections.com/NJ/"
# County -> its Clarity election (the site's own path).
ELECTIONS = {
    "atlantic": "Atlantic/127487", "burlington": "Burlington/127582",
    "cape_may": "Cape_May/127585", "cumberland": "Cumberland/127562",
    "essex": "Essex/127486", "gloucester": "Gloucester/127298", "hudson": "Hudson/127495",
    "mercer": "Mercer/127554", "middlesex": "Middlesex/127580", "monmouth": "Monmouth/127586",
    "morris": "Morris/127576", "ocean": "Ocean/127556", "passaic": "Passaic/127401",
    "somerset": "Somerset/127569", "union": "Union/127567",
}  # fmt: skip
FILES = {
    "camden.pdf": "https://www.camdencounty.com/wp-content/elections/general2026/2026_General_Election_Canvasser.pdf",
    "sussex.pdf": "https://www.sussexcountyclerk.org/DocumentCenter/View/4874/2026-NOV-3-GE---OFFICIAL-results---countywide-SUPP",
    "salem.html": "https://www.livevoterturnout.com/ENR/salemnjenr/13/en/Index_13.html",
}  # fmt: skip
SAVED = {"bergen": "bergen.pdf", "hunterdon": "hunterdon.pdf", "warren": "warren.pdf"}


def get(url, agent):
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise ValueError(f"{url}: HTTP {response.status}")
        body = response.read()
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--saved", action="append", default=[], metavar="COUNTY=PATH")
    parser.add_argument(
        "--election",
        action="append",
        default=[],
        metavar="COUNTY=PATH",
        help="Add or replace a Clarity county, e.g. passaic=Passaic/127401",
    )
    args = parser.parse_args()
    settings, _ = load_config(ROOT / "config/sources.toml")
    elections = dict(ELECTIONS)
    for item in args.election:
        county, path = item.split("=", 1)
        elections[county] = path
    saved = dict(item.split("=", 1) for item in args.saved)
    if set(saved) != set(SAVED):
        raise SystemExit(f"Saved files are needed for: {', '.join(sorted(SAVED))}")
    members, sources = {}, {}

    def add(name, body, origin):
        members[name] = body
        sources[name] = {
            "source": origin,
            "sha256": hashlib.sha256(body).hexdigest(),
            "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    for county, path in sorted(elections.items()):
        version = get(CLARITY + path + "/current_ver.txt", settings.user_agent).decode().strip()
        if not version.isdigit():
            raise SystemExit(f"{county}: unexpected Clarity version {version!r}")
        base = f"{CLARITY}{path}/{version}/json/en/"
        for kind, name in (("settings", "electionsettings.json"), ("summary", "summary.json")):
            add(f"clarity/{county}.{kind}.json", get(base + name, settings.user_agent), base + name)
            time.sleep(1)
    for name, url in FILES.items():
        add(name, get(url, settings.user_agent), url)
        time.sleep(1)
    for county, path in saved.items():
        add(SAVED[county], Path(path).read_bytes(), "saved by the operator in a browser")
    listing = json.dumps(sources, indent=1, sort_keys=True).encode()
    with zipfile.ZipFile(args.out, "w") as archive:
        for name, body in [*sorted(members.items()), ("sources.json", listing)]:
            # A ZipInfo is stored uncompressed unless told otherwise.
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    print(
        json.dumps(
            {"members": len(members), "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest()}
        )
    )


if __name__ == "__main__":
    main()
