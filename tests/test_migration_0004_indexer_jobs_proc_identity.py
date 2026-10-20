# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0004_indexer_jobs_proc_identity (#383 item 4) - schema behaviour.

- the two identity columns exist with their types after run_migrations, are
  NULL on a legacy-style row, and survive a re-run and a yoyo rollback +
  re-apply;
- the real update_job writes them, and still writes the rest of the row when
  they do not exist (code deployed before the migration);
- the alert hook's admin query returns exactly the active admins with an email.
"""
from __future__ import annotations

import pytest

from src.db import job_registry
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _columns(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_name = 'indexer_jobs'"
        )
        return dict(cur.fetchall())


@pytest.fixture
def migrated(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def test_identity_columns_exist_nullable_and_legacy_rows_stay_null(migrated):
    cols = _columns(migrated)
    assert cols.get("proc_start_ticks") == "bigint", cols
    assert cols.get("boot_id") == "text", cols
    job_id = _one(migrated, "INSERT INTO indexer_jobs (profile_name) VALUES ('p') RETURNING id")[0]
    assert _one(
        migrated, "SELECT proc_start_ticks, boot_id FROM indexer_jobs WHERE id = %s", (job_id,),
    ) == (None, None)


def test_rerun_and_rollback_then_reapply(migrated):
    from yoyo import get_backend, read_migrations

    from src.db.migrate import _MIGRATIONS_DIR, _conn_to_uri

    body = (_MIGRATIONS_DIR / "0004_indexer_jobs_proc_identity.sql").read_text()
    with migrated.cursor() as cur:
        cur.execute(body)  # idempotent by hand too
    run_migrations(migrated)
    backend = get_backend(_conn_to_uri(migrated))
    migrations = read_migrations(str(_MIGRATIONS_DIR))
    target = migrations.filter(lambda m: m.id == "0004_indexer_jobs_proc_identity")
    assert target, "0004 migration file not found"
    with backend.lock():
        backend.rollback_migrations(backend.to_rollback(target))
    assert not {"proc_start_ticks", "boot_id"} & set(_columns(migrated))
    assert "status" in _columns(migrated)  # the table itself is untouched
    run_migrations(migrated)
    assert {"proc_start_ticks", "boot_id"} <= set(_columns(migrated))


def test_update_job_writes_the_identity(migrated):
    migrated.autocommit = True
    job_id = _one(migrated, "INSERT INTO indexer_jobs (profile_name) VALUES ('p') RETURNING id")[0]
    job_registry.update_job(
        migrated, job_id, status="running", pid=4242, proc_start_ticks=987654321,
        boot_id="b0b0-1",
    )
    assert _one(
        migrated, "SELECT status, pid, proc_start_ticks, boot_id FROM indexer_jobs WHERE id = %s",
        (job_id,),
    ) == ("running", 4242, 987654321, "b0b0-1")


@pytest.mark.parametrize("autocommit", [True, False])
def test_update_job_before_the_migration_still_reports_running(migrated, autocommit):
    """Code deployed before 0004 is applied: the run must still report running."""
    migrated.autocommit = True
    with migrated.cursor() as cur:
        cur.execute("ALTER TABLE indexer_jobs DROP COLUMN proc_start_ticks")
        cur.execute("ALTER TABLE indexer_jobs DROP COLUMN boot_id")
    job_id = _one(migrated, "INSERT INTO indexer_jobs (profile_name) VALUES ('p') RETURNING id")[0]
    migrated.autocommit = autocommit
    job_registry.update_job(
        migrated, job_id, status="running", pid=4242, proc_start_ticks=1, boot_id="x",
    )
    if not autocommit:
        migrated.commit()
    migrated.autocommit = True
    assert _one(
        migrated, "SELECT status, pid FROM indexer_jobs WHERE id = %s", (job_id,),
    ) == ("running", 4242)


def test_alert_admin_query_returns_only_active_admins_with_an_email(migrated, monkeypatch):
    from src.ops import alert_notify
    from tests.conftest import get_test_dsn

    migrated.autocommit = True
    rows = [
        # (id, username, is_admin, is_active, email)
        (9101, "adm_ok", True, True, "ok@example.com"),
        (9102, "adm_off", True, False, "off@example.com"),
        (9103, "user", False, True, "user@example.com"),
        (9104, "adm_noemail", True, True, None),
        (9105, "adm_blank", True, True, "  "),
        (9106, "adm_ok2", True, True, "ok2@example.com"),
    ]
    with migrated.cursor() as cur:
        for uid, name, admin, active, email in rows:
            cur.execute(
                "INSERT INTO webui_users (id, username, is_admin, is_active, email)"
                " VALUES (%s, %s, %s, %s, %s)",
                (uid, name, admin, active, email),
            )
    monkeypatch.setenv("PG_DSN", get_test_dsn())
    assert alert_notify._fetch_admin_emails() == ["ok@example.com", "ok2@example.com"]
