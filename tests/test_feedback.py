# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_feedback.py
"""Integration tests for pattern_feedback CRUD.

Requires a live PostgreSQL connection — skipped automatically if not reachable.
Mark: pytest.mark.postgres (custom marker; any test that needs a real pg_conn).
"""
import pytest

from src.db.pg import auth_store

pytestmark = pytest.mark.postgres


def test_create_and_list_feedback(pg_conn):
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    fid = auth_store().create_feedback(
        pattern_node_id="test.pattern.1",
        api_key_id=None,
        rating="up",
        comment="Great!",
    )
    assert isinstance(fid, int), f"Expected int id, got {fid!r}"

    results = auth_store().list_feedback("test.pattern.1")
    assert len(results) >= 1
    # Find the row we just inserted
    our_row = next((r for r in results if r["id"] == fid), None)
    assert our_row is not None, "Inserted row not found in list"
    assert our_row["rating"] == "up"
    assert our_row["comment"] == "Great!"
    assert our_row["api_key_id"] is None


def test_create_feedback_down_rating(pg_conn):
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    fid = auth_store().create_feedback(
        pattern_node_id="test.pattern.2",
        api_key_id=None,
        rating="down",
        comment="Did not help.",
    )
    assert isinstance(fid, int)

    results = auth_store().list_feedback("test.pattern.2")
    our = next((r for r in results if r["id"] == fid), None)
    assert our is not None
    assert our["rating"] == "down"


def test_list_feedback_empty(pg_conn):
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    results = auth_store().list_feedback("test.pattern.nonexistent.xyz")
    assert isinstance(results, list)
    assert len(results) == 0


def test_feedback_invalid_rating_rejected(pg_conn):
    """CHECK constraint should reject invalid rating values."""
    import psycopg2

    from src.db.migrate import run_migrations

    run_migrations(pg_conn)

    with pytest.raises((psycopg2.errors.CheckViolation, psycopg2.IntegrityError)):
        auth_store().create_feedback(
            pattern_node_id="test.pattern.3",
            api_key_id=None,
            rating="invalid",
        )
    # Rollback after failed transaction so pg_conn stays usable
    try:
        pg_conn.rollback()
    except Exception:
        pass
