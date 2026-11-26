"""Election-specific candidate and eligible-county rosters pinned from official snapshots."""

from contextlib import contextmanager
from contextvars import ContextVar

from election_pipeline.parsers.rosters import ROSTERS

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


def verify_scope(code, race, candidates, counties=None, *, collected=False):
    """``collected``: the counties are those collected so far (``State.collected_counties``),
    so they must be reviewed counties of the race but need not be all of them."""
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
    if counties is not None and (
        not set(counties) <= set(expected["counties"])
        if collected
        else set(counties) != set(expected["counties"])
    ):
        raise ValueError(
            f"{race}: eligible county coverage differs from the reviewed election snapshot"
        )


def verify_rows(code, rows):
    """Check rows in the shared layout (``parsers.results.FIELDS``) against the state's
    roster: each race's choices, and the counties it has rows for (none for a state that
    publishes statewide totals only)."""
    choices, counties = {}, {}
    for row in rows:
        race = row["contest_id"]
        choices.setdefault(race, set()).add(row["choice"])
        if row["county"]:
            counties.setdefault(race, set()).add(row["county"])
    for race in sorted(choices):
        found = counties.get(race)
        verify_scope(code, race, sorted(choices[race]), sorted(found) if found else None)
