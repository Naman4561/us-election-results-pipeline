"""Validate a saved Texas response without network access or a database connection."""

import argparse
import csv
import json
import tempfile
import zipfile
from pathlib import Path

from election_pipeline.parsing import parse_source
from election_pipeline.pipeline import collector_code_hash
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore, file_hash, utc_now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings, sources = load_config(root / "config/sources.toml")
    source = sources["tx_2026_general"]
    if args.file.stat().st_size > settings.max_download_bytes:
        raise ValueError("Texas input exceeds the download limit")
    with tempfile.TemporaryDirectory(prefix="texas-civix-check-") as directory:
        data = Path(directory)
        archive = data / "input.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(source.primary_filename, args.file.read_bytes())
        store = ArchiveStore(data, source)
        digest = file_hash(archive)
        target = store.archive_path(digest)
        target.parent.mkdir(parents=True)
        archive.replace(target)
        result = parse_source(source, settings, data, sha256=digest)
        report = {
            key: result.get(key)
            for key in (
                "outcome",
                "error",
                "source_version",
                "source_update_text",
                "all_counts_zero",
                "internal_reconciliation",
                "contest_total_reconciliation",
                "contest_count",
            )
        }
        report.update(
            source_id=source.source_id,
            source_signature=source.signature(settings),
            collector_code_sha256=collector_code_hash(),
            raw_sha256=file_hash(args.file),
            checked_at=utc_now(),
        )
        if result.get("outcome") == "parsed":
            with (Path(result["output_dir"]) / "contest_results.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                report["results"] = list(csv.DictReader(handle))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if result.get("outcome") == "parsed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
