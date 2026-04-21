"""Shared pytest fixtures.

A real Postgres connection is optional for P1 smoke tests. If
``DATABASE_URL`` is set in the environment we yield a live psycopg
connection; otherwise tests that require it are skipped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def pg_conn(database_url: str | None) -> Iterator[object]:
    if not database_url:
        pytest.skip("DATABASE_URL not set; skipping live-Postgres fixture")
    import psycopg

    with psycopg.connect(database_url) as conn:
        yield conn
