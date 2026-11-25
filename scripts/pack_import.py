"""Build a multi-file manual import's zip from the previous import, and check one.

Each import is the previous one with files added or replaced (see
``election_pipeline.packing`` for the layouts and the additions file). ``pack`` writes a
deterministic zip, so the same inputs always give the same SHA-256, and prints it for the
import; ``check`` verifies a zip against its layout and parses it offline as the pipeline
would, printing the outcome and statewide totals.

    python scripts/pack_import.py pack --source wi_2026_general --base wi_results_12.zip \\
        --add round13/additions.json --out wi_results_13.zip
    python scripts/pack_import.py check --source wi_2026_general wi_results_13.zip

Illinois's additions are built from its format-specific entries by
``scripts/il_bundle.py pack``, which packs through the same module.
"""

import argparse
import csv
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from election_pipeline import packing
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter

ROOT = Path(__file__).resolve().parents[1]


def layout(source):
    found = getattr(state_adapter(source.state).parser, "PACK", None)
    if found is None:
        raise SystemExit(f"{source.source_id} is not imported as a multi-file zip")
    return found


def pack(args, source):
    base = packing.read_zip(args.base.read_bytes()) if args.base else {}
    remove, files, entries = packing.additions(args.add) if args.add else ([], {}, {})
    members = packing.pack(
        layout(source), base, remove=[*remove, *args.remove], files=files, entries=entries
    )
    packing.write_zip(args.out, members)
    print(len(members), "members", packing.sha256(args.out.read_bytes()))


def check(args, source, settings):
    from election_pipeline.parsing import parse_source
    from election_pipeline.storage import ArchiveStore

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raw = args.zip.read_bytes()
    packing.check(layout(source), packing.read_zip(raw))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wrapped = io.BytesIO()
        with zipfile.ZipFile(wrapped, "w") as archive:
            archive.writestr(source.primary_filename, raw)
        digest = packing.sha256(wrapped.getvalue())
        target = ArchiveStore(root, source).archive_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(wrapped.getvalue())
        report = parse_source(source, settings, root, sha256=digest)
        print(report["outcome"], report.get("error", ""))
        if report["outcome"] != "parsed":
            raise SystemExit(1)
        print(report.get("source_update_text", ""))
        with (Path(report["output_dir"]) / "contest_results.csv").open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                print(row["contest_id"], row["choice"], row["total_votes"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    packer = sub.add_parser("pack", help="Write the next import zip")
    packer.add_argument("--source", required=True)
    packer.add_argument("--base", type=Path, help="The previous import zip")
    packer.add_argument("--add", type=Path, help="The additions file (JSON)")
    packer.add_argument("--remove", action="append", default=[], metavar="KEY_OR_MEMBER")
    packer.add_argument("--out", type=Path, required=True)
    checker = sub.add_parser("check", help="Verify a zip and parse it offline")
    checker.add_argument("--source", required=True)
    checker.add_argument("zip", type=Path)
    args = parser.parse_args()
    settings, sources = load_config(ROOT / "config")
    source = sources[args.source]
    try:
        if args.action == "pack":
            pack(args, source)
        else:
            check(args, source, settings)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps({"source": source.source_id, "action": args.action}))


if __name__ == "__main__":
    main()
