"""Describe an adapter from inside its image, without credentials or network access."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from election_pipeline.pipeline import collector_code_hash
from election_pipeline.settings import load_config
from election_pipeline.states import state_adapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    settings, sources = load_config(Path(__file__).resolve().parents[1] / "config/sources.toml")
    source = sources[args.source]
    state = state_adapter(source.state)
    if source.dataset != "election_results" or state.source_id != source.source_id:
        raise ValueError("Expansion accepts registered result adapters only")
    print(
        json.dumps(
            {
                "source": asdict(source),
                "settings": asdict(settings),
                "code_sha256": collector_code_hash(),
                "races": list(state.races),
            }
        )
    )


if __name__ == "__main__":
    main()
