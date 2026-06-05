# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_conftest_osm_reader_helper.py
"""Meta-tests for the shared osm_reader helper in conftest.py (#254, WI-10, M4).

These tests verify the *control flow* of `ensure_osm_reader_or_skip` — in
particular the no-CREATEROLE branch — WITHOUT a real Postgres connection. A
fake conn is injected whose cursor raises ``psycopg2.errors.InsufficientPrivilege``
on the CREATE ROLE statement, proving the helper turns that infra condition into
a ``pytest.skip`` (NOT a hard ERROR) per ADR-0040 precedent.

Without this test the no-CREATEROLE branch was never positively exercised — a
regression flipping skip→error would still pass CI on any superuser DB (#254
acceptance: "DB user thiếu CREATEROLE: 8 case SKIP, không ERROR").
"""
import psycopg2.errors
import pytest

from tests.conftest import ensure_osm_reader_or_skip


class _FakeCursor:
    """Context-manager cursor whose execute() raises a chosen exception."""

    def __init__(self, raise_exc: Exception | None):
        self._raise_exc = raise_exc
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        if self._raise_exc is not None:
            raise self._raise_exc


class _FakeConn:
    """Minimal psycopg2-conn stand-in recording commit/rollback calls."""

    def __init__(self, raise_exc: Exception | None = None):
        self._cursor = _FakeCursor(raise_exc)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_ensure_osm_reader_skips_when_no_createrole():
    """No CREATEROLE → InsufficientPrivilege → pytest.skip, conn rolled back.

    Business rule (#254): a DB user lacking CREATE ROLE is an infra limitation,
    not a code defect, so the grant-coverage tests must SKIP (not ERROR). This
    is the branch that would silently break if someone replaced pytest.skip with
    a raise.
    """
    insufficient = psycopg2.errors.InsufficientPrivilege("permission denied to create role")
    conn = _FakeConn(raise_exc=insufficient)

    # pytest.skip raises Skipped; assert the helper performs the skip on this
    # exact condition.
    with pytest.raises(pytest.skip.Exception):
        ensure_osm_reader_or_skip(conn)

    # The helper must roll back the aborted transaction before skipping so the
    # connection is reusable by teardown.
    assert conn.rolled_back is True
    assert conn.committed is False


def test_ensure_osm_reader_commits_on_success():
    """With CREATEROLE present (no exception) → role created + committed, no skip.

    Confirms the happy path does NOT skip and DOES commit so the role is visible
    inside the subsequent migration transaction (the assertion #254 protects on
    a superuser/CREATEROLE DB).
    """
    conn = _FakeConn(raise_exc=None)

    # Must not raise Skipped (or anything).
    ensure_osm_reader_or_skip(conn)

    assert conn.committed is True
    assert conn.rolled_back is False
    # The CREATE ROLE DO-block must actually have been issued.
    assert any("CREATE ROLE osm_reader" in sql for sql in conn._cursor.executed)
