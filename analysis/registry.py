"""What the pipeline's state registry (src/election_pipeline/states.py) says, for analysis.

Some states have an older election-night source beside their current one (Minnesota,
Mississippi, Nebraska, North Carolina and Texas), and North Carolina a demographics
dataset; each still has a current publication. The registry names the one results
source of each state, recorded in ``ref.state_sources`` so analysis reads that alone.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def states():
    sys.path.insert(0, str(ROOT / "src"))
    from election_pipeline.states import STATES

    return STATES


def sync(conn):
    """Rewrite ref.state_sources from the registry of this checkout."""
    with conn.transaction():
        conn.execute("DELETE FROM ref.state_sources")
        with conn.cursor().copy(
            "COPY ref.state_sources (state, source_id, local_unit, county_results) FROM STDIN"
        ) as copy:
            for code, state in sorted(states().items()):
                copy.write_row((code, state.source_id, state.local_unit, state.county_results))
