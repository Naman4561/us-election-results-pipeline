"""Assemble Illinois's ``results.zip`` for a manual import, and write and sign its reviews.

Illinois's results come authority by authority (``parsers/il.py``), so each import is
the previous one with some authorities added or replaced. ``pack`` therefore starts
from the previous import (``--base``), whose manifest already records every authority's
files, SHA-256s, addresses and times, and applies an additions file: JSON naming, for
each authority added or replaced, its format and the files saved for it (paths relative
to the additions file). It writes a deterministic zip, so the same inputs always give
the same SHA-256, and prints it for the import.

    python scripts/il_bundle.py pack --base il_results_9.zip --add round10.json \\
        --out il_results_10.zip
    python scripts/il_bundle.py review Will.spec.json Will_canvass.pdf --out reviews
    python scripts/il_bundle.py sign reviews Naman 2026-11-24 Will
    python scripts/il_bundle.py check il_results_10.zip

An additions entry is one of (``report`` is ``canvassed`` or ``unofficial``; ``saved_at``
defaults to the newest file's modification time, ``report_time`` to null):

    {"format": "clarity", "report": ..., "site": ..., "path": "IL/Lake/127289",
     "version": "387162", "settings": file, "summary": file, "host": optional}
    {"format": "platinum", "report": ..., "host": ..., "id": 76, "pages": [p1, p2, p3]}
    {"format": "pollresults" | "livevoterturnout" | "gbs" | "enhanced_voting",
     "report": ..., "url": ..., "file": file}
    {"format": "chicago", "report": ..., "url": ..., "races": {race: file},
     "contests": {race: contest number}}
    {"format": "review", "report": ..., "url": ..., "files": [file, ...],
     "review": signed review file}

``review`` writes an unsigned ``<Authority>.review.json`` from a transcription (JSON:
``authority``, ``report``, ``transcribed_by``, ``races`` {race: {line: votes}}, and
optionally ``totals`` {race: printed total}, ``precincts`` {race: [reporting, total]} and
``percentages`` {race: {line: printed percent}}), refusing counts that disagree with what
the report prints. ``sign`` records the operator who checked it; the parser refuses an
unsigned review or one for other files. ``check`` parses a zip as the pipeline would,
offline, and prints its outcome and statewide totals.
"""

import argparse
import csv
import hashlib
import io
import json
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLARITY = "https://results.enr.clarityelections.com"
# Single-page formats: the member folder and extension of each.
PAGES = {
    "livevoterturnout": ("lvt", "html"),
    "pollresults": ("pollresults", "html"),
    "gbs": ("gbs", "json"),
    "enhanced_voting": ("ev", "json"),
}
REPORTS = ("canvassed", "unofficial")


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def saved_time(paths):
    newest = max(path.stat().st_mtime for path in paths)
    return datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")


def entry_for(authority, item, folder):
    """({member: bytes}, manifest entry) for one authority of an additions file."""
    slug = authority.replace(" ", "_").replace(".", "")
    form, report = item.get("format"), item.get("report")
    if report not in REPORTS:
        raise SystemExit(f"{authority}: report must be one of {REPORTS}")

    def read(name):
        path = folder / name
        if not path.is_file():
            raise SystemExit(f"{authority}: no file {path}")
        return path, path.read_bytes()

    members, parts, paths = {}, [], []

    def add(member, name, **details):
        path, body = read(name)
        members[member] = body
        paths.append(path)
        parts.append({**details, "file": member, "sha256": sha256(body)})

    entry = {"format": form, "report": report}
    if form == "clarity":
        base = f"{item.get('host', CLARITY)}/{item['path']}/{item['version']}/json/en/"
        for role, name in (("settings", "electionsettings.json"), ("summary", "summary.json")):
            add(f"clarity/{slug}.{role}.json", item[role], role=role, url=base + name)
        entry.update(site=item["site"], url=base + "summary.json")
    elif form == "platinum":
        if len(item["pages"]) != 3:
            raise SystemExit(f"{authority}: a platinum county is its three race pages")
        for category, name in enumerate(item["pages"], 1):
            url = f"https://{item['host']}/races/{item['id']}/{category}"
            add(f"platinum/{slug}.races{category}.html", name, role=f"races{category}", url=url)
        entry.update(url=f"https://{item['host']}/races/{item['id']}/1")
    elif form in PAGES:
        directory, extension = PAGES[form]
        add(f"{directory}/{slug}.{extension}", item["file"], role="page", url=item["url"])
        entry.update(url=item["url"])
    elif form == "chicago":
        for race, name in sorted(item["races"].items()):
            add(
                f"chicago/{race}.json",
                name,
                role=race,
                url=item["url"],
                contest=item["contests"][race],
            )
        entry.update(url=item["url"])
    elif form == "review":
        files = item["files"]
        for index, name in enumerate(files, 1):
            suffix = Path(name).suffix.lower()
            add(f"{slug}_{index}{suffix}" if len(files) > 1 else f"{slug}{suffix}", name)
        _, body = read(item["review"])
        from election_pipeline import reviews

        review = json.loads(body)
        listed = reviews.files(review)
        if reviews.place(review) != authority or listed != [p["sha256"] for p in parts]:
            raise SystemExit(f"{authority}: the review is for another authority or file")
        if not reviews.signer(review):
            raise SystemExit(f"{authority}: the review is not signed")
        members[f"{authority}.review.json"] = body
        entry.update(url=item["url"])
    else:
        raise SystemExit(f"{authority}: unknown format {form!r}")
    entry.update(
        files=parts,
        saved_at=item.get("saved_at") or saved_time(paths),
        report_time=item.get("report_time"),
    )
    return members, entry


def pack(args):
    from election_pipeline import packing
    from election_pipeline.parsers.il import PACK

    base = packing.read_zip(args.base.read_bytes()) if args.base else {}
    additions = json.loads(args.add.read_text(encoding="utf-8")) if args.add else {}
    files, entries = {}, {}
    for authority, item in sorted(additions.items()):
        members, entry = entry_for(authority, item, args.add.parent)
        files.update(members)
        entries[authority] = entry
    try:
        members = packing.pack(PACK, base, remove=args.remove, files=files, entries=entries)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    manifest = json.loads(members["manifest.json"])["authorities"]
    packing.write_zip(args.out, members)
    digest = sha256(args.out.read_bytes())
    print(len(manifest), "authorities", len(members), "members", digest)


def review(args):
    """An unsigned review of an authority's report, through the one review tool."""
    from election_pipeline import reviews

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    spec = {**spec, "state": "IL", "place": spec.get("place", spec.get("authority"))}
    try:
        document = reviews.transcription(spec, [sha256(p.read_bytes()) for p in args.files])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{document['place']}.review.json"
    if path.exists() and reviews.signer(json.loads(path.read_text(encoding="utf-8"))):
        raise SystemExit(f"{path.name} is signed; write a new review under another folder")
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False), encoding="utf-8")
    print("wrote", path.name, {race: sum(lines.values()) for race, lines in spec["races"].items()})


def sign(args):
    from election_pipeline import reviews

    for authority in args.authorities:
        path = args.folder / f"{authority}.review.json"
        try:
            document = reviews.signed(
                json.loads(path.read_text(encoding="utf-8")), args.signer, args.date
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
        path.write_text(json.dumps(document, indent=1, ensure_ascii=False), encoding="utf-8")
        print("signed", path.name)


def check(args):
    from election_pipeline.parsing import parse_source
    from election_pipeline.settings import load_config
    from election_pipeline.storage import ArchiveStore

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings, sources = load_config(ROOT / "config")
    source = sources["il_2026_general"]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wrapped = io.BytesIO()
        with zipfile.ZipFile(wrapped, "w") as archive:
            archive.writestr(source.primary_filename, args.zip.read_bytes())
        digest = sha256(wrapped.getvalue())
        target = ArchiveStore(root, source).archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(wrapped.getvalue())
        report = parse_source(source, settings, root, sha256=digest)
        print(report["outcome"], report.get("error", ""))
        if report["outcome"] != "parsed":
            raise SystemExit(1)
        print(report["source_update_text"])
        print("waiting:", ", ".join(report["counties_waiting"]) or "none")
        with (Path(report["output_dir"]) / "contest_results.csv").open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                print(row["contest_id"], row["choice"], row["total_votes"])


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("pack", help="write results.zip")
    command.add_argument("--base", type=Path, help="the previous import's results.zip")
    command.add_argument("--add", type=Path, help="additions file (JSON)")
    command.add_argument("--remove", action="append", default=[], metavar="AUTHORITY")
    command.add_argument("--out", type=Path, required=True)
    command.set_defaults(run=pack)
    command = commands.add_parser("review", help="write an unsigned review")
    command.add_argument("spec", type=Path, help="the transcription (JSON)")
    command.add_argument("files", type=Path, nargs="+", help="the results file(s), in order")
    command.add_argument("--out", type=Path, required=True, help="reviews folder")
    command.set_defaults(run=review)
    command = commands.add_parser("sign", help="sign reviews")
    command.add_argument("folder", type=Path)
    command.add_argument("signer")
    command.add_argument("date", help="YYYY-MM-DD")
    command.add_argument("authorities", nargs="+")
    command.set_defaults(run=sign)
    command = commands.add_parser("check", help="parse a results.zip offline")
    command.add_argument("zip", type=Path)
    command.set_defaults(run=check)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
