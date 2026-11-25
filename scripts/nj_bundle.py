"""Assemble New Jersey's county results into one zip for a manual import.

New Jersey publishes no statewide results before certification: each of its 21 county
clerks posts its own. This operator tool downloads, through the pipeline's own client
(collect.fetch_file: its user agent, size and time limits, redirect checks and per-host
TLS; nothing solved or retried around a refusal), what answers: each
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
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from election_pipeline import packing
from election_pipeline.collect import fetch_file
from election_pipeline.parsers.nj import PACK
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


def get(url, source, settings):
    """One file, fetched as the collector fetches its sources (collect.fetch_file)."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "body"
        response = fetch_file(source, settings, target, {}, url=url)
        if response.status != 200:
            raise ValueError(f"{url}: HTTP {response.status}")
        body = target.read_bytes()
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
    settings, sources = load_config(ROOT / "config")
    source = sources["nj_2026_general"]
    settings = source.effective_settings(settings)
    elections = dict(ELECTIONS)
    for item in args.election:
        county, path = item.split("=", 1)
        elections[county] = path
    saved = dict(item.split("=", 1) for item in args.saved)
    if set(saved) != set(SAVED):
        raise SystemExit(f"Saved files are needed for: {', '.join(sorted(SAVED))}")
    members, listing = {}, {}

    def add(name, body, origin):
        members[name] = body
        listing[name] = {
            "source": origin,
            "sha256": hashlib.sha256(body).hexdigest(),
            "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    for county, path in sorted(elections.items()):
        version = get(CLARITY + path + "/current_ver.txt", source, settings).decode().strip()
        if not version.isdigit():
            raise SystemExit(f"{county}: unexpected Clarity version {version!r}")
        base = f"{CLARITY}{path}/{version}/json/en/"
        for kind, name in (("settings", "electionsettings.json"), ("summary", "summary.json")):
            add(f"clarity/{county}.{kind}.json", get(base + name, source, settings), base + name)
            time.sleep(1)
    for name, url in FILES.items():
        add(name, get(url, source, settings), url)
        time.sleep(1)
    for county, path in saved.items():
        add(SAVED[county], Path(path).read_bytes(), "saved by the operator in a browser")
    members["sources.json"] = packing.document(listing)
    packing.check(PACK, members)
    packing.write_zip(args.out, members)
    print(
        json.dumps(
            {"members": len(members), "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest()}
        )
    )


if __name__ == "__main__":
    main()
