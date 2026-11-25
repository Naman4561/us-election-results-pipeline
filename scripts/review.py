"""Write, sign and check a review of an official report no reader takes.

A review is an operator's transcription of a report's counts, bound to the report's
exact bytes and signed by whoever checked it against the report (see
``election_pipeline.reviews`` for the format). Every state that takes reviews uses this
tool; the signed review then goes into the state's import zip beside the report
(``scripts/pack_import.py``, ``scripts/il_bundle.py``).

    python scripts/review.py write Vilas.spec.json Vilas_part1.pdf Vilas_part2.pdf --out reviews
    python scripts/review.py sign reviews/Vilas.review.json --signer Naman --date 2026-11-24
    python scripts/review.py check reviews/Vilas.review.json Vilas_part1.pdf Vilas_part2.pdf

``write`` refuses counts that disagree with the totals or percentages the report
prints; ``sign`` signs once; ``check`` reads a review as the parsers do.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from election_pipeline import reviews


def save(path, document):
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def write(spec_path, files, folder):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    digests = [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files]
    document = reviews.transcription(spec, digests)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{document['place']}.review.json"
    if path.exists() and reviews.signer(json.loads(path.read_text(encoding="utf-8"))):
        raise ValueError(f"{path.name} is signed; write a new review under another folder")
    save(path, document)
    return path, {race: sum(lines.values()) for race, lines in document["races"].items()}


def sign(path, signer, day):
    path = Path(path)
    save(path, reviews.signed(json.loads(path.read_text(encoding="utf-8")), signer, day))
    return path


def check(path, files):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    digests = [hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files]
    found = reviews.read(document, digests, reviews.place(document) or Path(path).name)
    return {race: sum(v for _, v in lines) for race, (lines, _) in found.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    writer = sub.add_parser("write", help="Write an unsigned review from a transcription")
    writer.add_argument("spec", type=Path)
    writer.add_argument("files", type=Path, nargs="+", help="The report, or its parts in order")
    writer.add_argument("--out", type=Path, required=True)
    signer = sub.add_parser("sign", help="Sign a review once it has been checked")
    signer.add_argument("review", type=Path)
    signer.add_argument("--signer", required=True)
    signer.add_argument("--date", required=True, help="YYYY-MM-DD")
    checker = sub.add_parser("check", help="Read a review as the parsers do")
    checker.add_argument("review", type=Path)
    checker.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        if args.action == "write":
            path, totals = write(args.spec, args.files, args.out)
            print("wrote", path.name, json.dumps(totals))
        elif args.action == "sign":
            print("signed", sign(args.review, args.signer, args.date).name)
        else:
            print("checked", json.dumps(check(args.review, args.files)))
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
