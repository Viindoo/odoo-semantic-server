# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-unit tests for the ADR-0007 W14 version-scope fix.

These tests are DB-free (pool mocked) so they run in the local unit lane
(`make test-unit`, i.e. `-m "not neo4j and not postgres"`). They protect the
contract of the fix without a live PostgreSQL:

- ``get_repo_ids_by_local_path_basenames`` REQUIRES an ``odoo_version`` argument
  and threads it into the SQL parameters.
- The emitted query joins ``profiles`` and constrains ``p.odoo_version`` - the
  predicate that eliminates the cross-version over-reset (W14).
- Empty basenames short-circuit without touching the pool.

The behavioural DB-backed regression/safety-net/e2e tests live in
``tests/test_cross_repo_dep_propagation.py`` (neo4j/postgres-marked, run by CI).
"""
from unittest.mock import MagicMock

import pytest

from src.db.repo_registry import RepoStore


def _make_store_with_rows(rows: list[dict]) -> tuple[RepoStore, MagicMock]:
    """Build a RepoStore whose pool.fetch_all returns *rows*.

    Returns (store, pool) so callers can inspect pool.fetch_all.call_args.
    """
    pool = MagicMock()
    pool.fetch_all.return_value = rows
    return RepoStore(pool), pool


def test_version_is_threaded_into_query_params():
    """odoo_version is passed through as the second SQL parameter."""
    store, pool = _make_store_with_rows([{"id": 7}])

    result = store.get_repo_ids_by_local_path_basenames(["tvtmaaddons"], "17.0")

    assert result == [7]
    # fetch_all(conn, sql, params) - inspect the params tuple.
    _conn, sql, params = pool.fetch_all.call_args.args
    assert params == (["tvtmaaddons"], "17.0"), (
        f"Expected basenames + version threaded into params; got: {params!r}"
    )
    assert "p.odoo_version = %s" in sql, (
        f"Query must constrain profiles.odoo_version; got SQL: {sql}"
    )
    assert "JOIN profiles" in sql, (
        f"Query must join profiles to reach odoo_version; got SQL: {sql}"
    )


def test_empty_basenames_short_circuits_without_db():
    """Empty basenames returns [] and never touches the pool."""
    store, pool = _make_store_with_rows([])

    result = store.get_repo_ids_by_local_path_basenames([], "17.0")

    assert result == []
    pool.checkout.assert_not_called()
    pool.fetch_all.assert_not_called()


def test_odoo_version_argument_is_required():
    """Calling without odoo_version is a TypeError (regression guard).

    Locks the new required parameter so a caller cannot silently revert to the
    version-blind lookup that caused the W14 cross-version over-reset.
    """
    store, _pool = _make_store_with_rows([{"id": 1}])

    with pytest.raises(TypeError):
        store.get_repo_ids_by_local_path_basenames(["tvtmaaddons"])  # type: ignore[call-arg]
