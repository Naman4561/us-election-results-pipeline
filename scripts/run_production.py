"""Run configured result sources through the shared workflow."""

import json
import os
from pathlib import Path

from election_pipeline.settings import load_config
from election_pipeline.workflow import run_source


def selected_sources(configured, names):
    keys = [name.strip() for name in names.split(",") if name.strip()]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("PIPELINE_SOURCES must list unique source IDs")
    if any(key not in configured for key in keys):
        raise ValueError("PIPELINE_SOURCES contains an unknown source ID")

    selected = [configured[key] for key in keys]
    if any(source.dataset != "election_results" for source in selected):
        raise ValueError(
            "The recurring job selects results only; collect demographics manually"
        )
    return selected


def main():
    root = Path(__file__).resolve().parents[1]
    settings, configured = load_config(root / "config/sources.toml")
    sources = selected_sources(
        configured,
        os.environ.get("PIPELINE_SOURCES", ""),
    )
    status = os.environ.get("PIPELINE_STATUS", "test")
    if status not in ("test", "unofficial"):
        raise ValueError("PIPELINE_STATUS must be test or unofficial")

    code = 0
    for source in sources:
        # Publication currently uses a shared lock, so process serially.
        try:
            result = run_source(
                source,
                settings,
                root / "data",
                root=root,
                status=status,
            )
        except Exception as error:
            password = os.environ.get("POSTGRES_PASSWORD", "")
            message = str(error)
            if password:
                message = message.replace(password, "[redacted]")
            result = {
                "source_id": source.source_id,
                "outcome": "failed",
                "error": message,
            }

        print(json.dumps(result, sort_keys=True), flush=True)
        if result["outcome"] not in {
            "succeeded",
            "already_current",
            "not_due",
            "awaiting_publication",
        }:
            code = 1

    return code


if __name__ == "__main__":
    raise SystemExit(main())