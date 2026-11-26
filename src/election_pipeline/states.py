"""Every covered state: its races, vote methods and parser, shared by jobs and the dashboard.

The registry is built from the election in force (``config/elections/current``), whose
definition says each state's races, source and policies; ``State`` is in ``state_model``.
"""

from election_pipeline import elections
from election_pipeline.state_model import State

__all__ = ["ELECTION", "STATES", "State", "parser_for", "publication_lock", "state_adapter"]

ELECTION = elections.load()
STATES = ELECTION.states


def state_adapter(code):
    if code not in STATES:
        raise ValueError(f"No state adapter registered for {code}")
    return STATES[code]


def parser_for(source):
    adapter = state_adapter(source.state)
    return adapter.parser.parse


def publication_lock(conn, state):
    # A shared maintenance lock allows state jobs to overlap while backups stay exclusive.
    if not conn.execute("SELECT pg_try_advisory_lock_shared(20261103, 1) AS ok").fetchone()["ok"]:
        raise ValueError("Backup is already running; retry this state later")
    key = int.from_bytes(state.encode("ascii"), "big")
    if not conn.execute("SELECT pg_try_advisory_lock(20261104, %s) AS ok", (key,)).fetchone()["ok"]:
        raise ValueError(f"Another {state} publication or restore is running")
