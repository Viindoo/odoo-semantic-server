"""Shared pytest fixtures.

A real Postgres connection is optional. If ``DATABASE_URL`` is set in the
environment we yield a live psycopg connection; otherwise tests that
require it are skipped.

``ODOO_SOURCE_PATH`` is an optional path to a local Odoo checkout used by
benchmark tests that parse real Odoo source. When unset, those tests skip.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def odoo_source_path() -> pathlib.Path | None:
    path = os.environ.get("ODOO_SOURCE_PATH")
    return pathlib.Path(path) if path else None


@pytest.fixture(scope="session")
def pg_conn(database_url: str | None) -> Iterator[object]:
    if not database_url:
        pytest.skip("DATABASE_URL not set; skipping live-Postgres fixture")
    import psycopg

    with psycopg.connect(database_url) as conn:
        yield conn
