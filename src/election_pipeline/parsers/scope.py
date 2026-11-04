"""Election-specific candidate and eligible-county rosters pinned from official snapshots."""

from election_pipeline.parsers.scope_2026 import ROSTERS


def verify_scope(code, race, candidates, counties=None):
    expected = ROSTERS[code][race]
    if len(candidates) != len(set(candidates)) or set(candidates) != set(expected["candidates"]):
        raise ValueError(f"{race}: candidate roster differs from the reviewed election snapshot")
    if counties is not None and set(counties) != set(expected["counties"]):
        raise ValueError(
            f"{race}: eligible county coverage differs from the reviewed election snapshot"
        )
