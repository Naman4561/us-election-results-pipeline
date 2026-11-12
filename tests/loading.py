"""Shared test helpers: the migrated test database, and what a release would load."""

import os
import unittest
from pathlib import Path

from election_pipeline.database import connect, connection_settings, converted, verify
from election_pipeline.migrations import migrate
from election_pipeline.states import state_adapter


def requires_database(cls):
    """Run ``cls`` only against the disposable ``*_test`` database. ``check_pipeline.py
    --database-only`` runs just these classes; the plain unit-test jobs skip them."""
    cls.requires_database = True
    return unittest.skipUnless(
        os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable PostgreSQL *_test"
    )(cls)


def copies(source, settings, store, directory, release=1):
    """{relation: [row tuples]} after the same verification the loader performs."""
    _, _, _, plan = verify(source, settings, store, Path(directory))
    methods = state_adapter(source.state).methods[1:]
    return {
        relation: list(rows)
        for relation, _, rows in converted(source, Path(directory), release, plan, methods)
    }


def migrated():
    """Settings for the disposable ``*_test`` database, with every migration applied."""
    name = os.environ["ELECTION_TEST_DATABASE"]
    if not name.endswith("_test"):
        raise ValueError("Integration tests require a database name ending in _test")
    db = {**connection_settings(), "dbname": name}
    with connect(db) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='election_dashboard'").fetchone():
            conn.execute("CREATE ROLE election_dashboard NOLOGIN")
        migrate(conn)
    return db
