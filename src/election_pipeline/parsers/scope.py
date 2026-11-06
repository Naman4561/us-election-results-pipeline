"""Election-specific candidate and eligible-county rosters pinned from official snapshots."""

from contextlib import contextmanager
from contextvars import ContextVar

from election_pipeline.parsers.scope_2026 import ROSTERS

_recording = ContextVar("roster_recording", default=None)


@contextmanager
def recording():
    """Record what a parse observes instead of checking it (roster review only).

    Yields {state: {race: {"candidates": [...], "counties": [...]}}}. Never used by the
    pipeline; ``scripts/review_rosters.py`` uses it to propose a roster for review.
    """
    observed = {}
    token = _recording.set(observed)
    try:
        yield observed
    finally:
        _recording.reset(token)


def verify_scope(code, race, candidates, counties=None):
    observed = _recording.get()
    if observed is not None:
        observed.setdefault(code, {})[race] = {
            "candidates": list(candidates),
            "counties": sorted(counties) if counties is not None else [],
        }
        return
    expected = ROSTERS[code][race]
    if len(candidates) != len(set(candidates)) or set(candidates) != set(expected["candidates"]):
        raise ValueError(f"{race}: candidate roster differs from the reviewed election snapshot")
    if counties is not None and set(counties) != set(expected["counties"]):
        raise ValueError(
            f"{race}: eligible county coverage differs from the reviewed election snapshot"
        )
