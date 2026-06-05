"""Unit tests for the destructive-DB safety guard in ``tests/conftest.py``.

These two pure functions (``_host_from_target`` and ``_assert_test_db_target_is_safe``)
gate the Neo4j/Postgres integration fixtures so they can never run ``DETACH DELETE`` /
``TRUNCATE`` against a REMOTE (production) store. The guard is safety-critical but was
DB-free and untested, so a future refactor could silently disable it. Each test below
pins one branch and fails independently if that branch's behaviour regresses.

DB-free by construction (the functions are pure / env-only) → runs in the unit tier,
no neo4j/postgres marker.
"""

import pytest

from tests.conftest import (
    _assert_test_db_target_is_safe,
    _host_from_target,
)

# --------------------------------------------------------------------------- #
# _host_from_target — host extraction across URI / DSN shapes
# --------------------------------------------------------------------------- #


def test_host_from_target_bolt_uri():
    """A bolt:// URI yields its hostname (lowercased), port stripped."""
    assert _host_from_target("bolt://db.prod.example:7687") == "db.prod.example"


def test_host_from_target_neo4j_secure_scheme():
    """The neo4j+s:// scheme is parsed like any other URL form."""
    assert _host_from_target("neo4j+s://Cluster.Example.COM") == "cluster.example.com"


def test_host_from_target_libpq_url_dsn():
    """URL-form libpq DSN (postgresql://user:pw@host:port/db) yields the host only."""
    assert (
        _host_from_target("postgresql://user:pw@db.prod.example:5432/odoo")
        == "db.prod.example"
    )


def test_host_from_target_libpq_keyword_dsn():
    """Keyword-form libpq DSN (host=... port=...) yields the host= token value."""
    assert (
        _host_from_target("host=db.prod.example port=5432 dbname=odoo")
        == "db.prod.example"
    )


def test_host_from_target_keyword_dsn_without_host_token():
    """A keyword DSN with no host= token cannot identify a host → '' (safe-by-omission)."""
    assert _host_from_target("port=5432 dbname=odoo") == ""


def test_host_from_target_empty_and_blank():
    """Empty / whitespace / None targets are undeterminable → '' (treated as loopback)."""
    assert _host_from_target("") == ""
    assert _host_from_target("   ") == ""
    assert _host_from_target(None) == ""


# --------------------------------------------------------------------------- #
# _assert_test_db_target_is_safe — fail-closed skip on positively-remote host
# --------------------------------------------------------------------------- #


def test_assert_safe_loopback_host_does_not_skip(monkeypatch):
    """A loopback target is always safe → the guard returns without skipping."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("OSM_ALLOW_REMOTE_TEST_DB", raising=False)
    monkeypatch.setenv("OSM_GUARD_TEST_TARGET", "bolt://localhost:7687")
    # Must NOT raise pytest.skip — reaching the assert past the call proves it.
    _assert_test_db_target_is_safe("OSM_GUARD_TEST_TARGET", "bolt://localhost:7687")


def test_assert_safe_remote_host_skips(monkeypatch):
    """A positively-remote, non-loopback target hard-skips (fail-closed)."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("OSM_ALLOW_REMOTE_TEST_DB", raising=False)
    monkeypatch.setenv("OSM_GUARD_TEST_TARGET", "bolt://db.prod.example:7687")
    with pytest.raises(pytest.skip.Exception):
        _assert_test_db_target_is_safe("OSM_GUARD_TEST_TARGET", "bolt://localhost:7687")


def test_assert_safe_ci_bypasses_remote_host(monkeypatch):
    """CI=true exempts even a remote target (service container is disposable)."""
    monkeypatch.delenv("OSM_ALLOW_REMOTE_TEST_DB", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("OSM_GUARD_TEST_TARGET", "bolt://db.prod.example:7687")
    # No skip despite the remote host — CI branch short-circuits before the host check.
    _assert_test_db_target_is_safe("OSM_GUARD_TEST_TARGET", "bolt://localhost:7687")


def test_assert_safe_optin_env_bypasses_remote_host(monkeypatch):
    """OSM_ALLOW_REMOTE_TEST_DB=1 lets an operator opt into a remote (disposable) store."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("OSM_ALLOW_REMOTE_TEST_DB", "1")
    monkeypatch.setenv("OSM_GUARD_TEST_TARGET", "bolt://db.prod.example:7687")
    # No skip — explicit opt-in short-circuits before the host check.
    _assert_test_db_target_is_safe("OSM_GUARD_TEST_TARGET", "bolt://localhost:7687")
