# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/db/job_registry.py — requires PostgreSQL."""
from datetime import UTC, datetime

import pytest

from src.db.migrate import run_migrations
from src.db.pg import job_store

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_jobs_conn(pg_conn):
    """Use the shared postgres fixture and ensure indexer_jobs table exists."""
    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM indexer_jobs")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM indexer_jobs")
    if not pg_conn.autocommit:
        pg_conn.commit()


class TestCreateAndGet:
    def test_create_returns_positive_id(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        assert isinstance(job_id, int)
        assert job_id > 0

    def test_create_default_status_queued(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        job = job_store().get_job(job_id)
        assert job is not None
        assert job["status"] == "queued"
        assert job["profile_name"] == "odoo17"
        assert job["pid"] is None
        assert job["started_at"] is None
        assert job["finished_at"] is None
        assert job["error_msg"] is None

    def test_get_missing_returns_none(self, pg_jobs_conn):
        result = job_store().get_job(999999)
        assert result is None

    def test_get_returns_all_columns(self, pg_jobs_conn):
        job_id = job_store().create_job("viin17")
        job = job_store().get_job(job_id)
        assert job is not None
        expected_keys = {
            "id", "profile_name", "status", "pid",
            "started_at", "finished_at", "error_msg", "created_at",
        }
        assert set(job.keys()) == expected_keys
        assert job["id"] == job_id
        assert job["profile_name"] == "viin17"
        # created_at should be a string (datetime converted to str)
        assert isinstance(job["created_at"], str)


class TestUpdateJob:
    def test_update_status_to_running(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        now = datetime.now(tz=UTC)
        job_store().update_job(job_id, status="running", pid=12345, started_at=now)
        job = job_store().get_job(job_id)
        assert job["status"] == "running"
        assert job["pid"] == 12345
        assert job["started_at"] is not None
        assert isinstance(job["started_at"], str)

    def test_update_status_to_done(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        now = datetime.now(tz=UTC)
        job_store().update_job(job_id, status="running", pid=42, started_at=now)
        job_store().update_job(job_id, status="done", finished_at=now)
        job = job_store().get_job(job_id)
        assert job["status"] == "done"
        assert job["finished_at"] is not None
        assert isinstance(job["finished_at"], str)

    def test_update_status_to_error_with_msg(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        now = datetime.now(tz=UTC)
        job_store().update_job(
            job_id,
            status="error",
            finished_at=now,
            error_msg="IndexError: something went wrong",
        )
        job = job_store().get_job(job_id)
        assert job["status"] == "error"
        assert job["error_msg"] == "IndexError: something went wrong"

    def test_update_invalid_status_raises(self, pg_jobs_conn):
        job_id = job_store().create_job("odoo17")
        with pytest.raises(ValueError, match="Invalid status"):
            job_store().update_job(job_id, status="invalid_status")

    def test_update_missing_job_raises(self, pg_jobs_conn):
        with pytest.raises(ValueError, match="Job 999999 not found"):
            job_store().update_job(999999, status="running")

    def test_update_partial_doesnt_clobber(self, pg_jobs_conn):
        """Updating only status should not clobber previously-set pid."""
        job_id = job_store().create_job("odoo17")
        now = datetime.now(tz=UTC)
        job_store().update_job(job_id, status="running", pid=7777, started_at=now)
        # Now update only status — pid should remain 7777
        job_store().update_job(job_id, status="done")
        job = job_store().get_job(job_id)
        assert job["status"] == "done"
        assert job["pid"] == 7777


class TestListAndLast:
    def test_list_running_filters(self, pg_jobs_conn):
        """3 jobs with different statuses — list_running_jobs returns only running ones."""
        now = datetime.now(tz=UTC)
        j1 = job_store().create_job("odoo17")
        j2 = job_store().create_job("odoo17")
        j3 = job_store().create_job("odoo17")
        job_store().update_job(j1, status="running", pid=101, started_at=now)
        job_store().update_job(j2, status="done")
        job_store().update_job(j3, status="error", error_msg="fail")

        running = job_store().list_running_jobs()
        assert len(running) == 1
        assert running[0]["id"] == j1
        assert running[0]["status"] == "running"

    def test_list_running_empty(self, pg_jobs_conn):
        result = job_store().list_running_jobs()
        assert result == []

    def test_get_last_job_orders_by_created_desc(self, pg_jobs_conn):
        """The most recently created job is returned."""
        job_store().create_job("odoo17")
        j2 = job_store().create_job("odoo17")
        # j2 was created after j1, so it should be the last
        last = job_store().get_last_job("odoo17")
        assert last is not None
        assert last["id"] == j2

    def test_get_last_job_filters_by_profile(self, pg_jobs_conn):
        """get_last_job only returns jobs for the given profile."""
        j_a = job_store().create_job("profile_a")
        j_b = job_store().create_job("profile_b")
        last_a = job_store().get_last_job("profile_a")
        last_b = job_store().get_last_job("profile_b")
        assert last_a["id"] == j_a
        assert last_b["id"] == j_b

    def test_get_last_job_missing_returns_none(self, pg_jobs_conn):
        result = job_store().get_last_job("nonexistent_profile")
        assert result is None
