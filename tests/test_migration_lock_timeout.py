# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema migrations that ALTER a live table must fail fast behind a lock holder.

Business rule: ``python -m src.db.migrate`` runs while the old Web UI is still
serving. An idle-in-transaction connection holding even ACCESS SHARE on
``indexer_jobs`` / ``module_presence`` would make the ALTER (ACCESS EXCLUSIVE)
wait forever and queue every later query on that table behind it (job pages
hang). Migrations 0004, 0006 and 0007 (and their rollbacks) therefore set a
transaction-scoped ``lock_timeout`` first: the migration is refused within
seconds, nothing is applied, and the operator re-runs it.
"""
from __future__ import annotations

import re
import time

import pytest

from src.db.migrate import _MIGRATIONS_DIR

_ALTERING = (
    "0004_indexer_jobs_proc_identity.sql",
    "0004_indexer_jobs_proc_identity.rollback.sql",
    "0006_module_presence_successor_merge.sql",
    "0006_module_presence_successor_merge.rollback.sql",
    "0007_module_presence_needs_rewrite_at.sql",
    "0007_module_presence_needs_rewrite_at.rollback.sql",
)


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


@pytest.mark.parametrize("name", _ALTERING)
def test_lock_timeout_is_set_before_any_lock_taking_statement(name):
    stmts = _statements((_MIGRATIONS_DIR / name).read_text())
    first_lock = next(
        i for i, s in enumerate(stmts) if re.match(r"(ALTER|UPDATE)\b", s, re.IGNORECASE)
    )
    guards = [s for s in stmts[:first_lock] if s.upper().startswith("SET LOCAL")]
    assert any(re.fullmatch(r"SET LOCAL lock_timeout = '\d+s'", g) for g in guards), stmts
    assert any(re.fullmatch(r"SET LOCAL statement_timeout = '\d+s'", g) for g in guards), stmts


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("name", "table"),
    [
        ("0004_indexer_jobs_proc_identity.sql", "indexer_jobs"),
        ("0006_module_presence_successor_merge.sql", "module_presence"),
        ("0007_module_presence_needs_rewrite_at.sql", "module_presence"),
    ],
)
def test_migration_gives_up_behind_an_idle_lock_holder(clean_pg, name, table):
    import psycopg2
    import psycopg2.errors

    from src.db.migrate import run_migrations
    from tests.conftest import get_test_dsn

    run_migrations(clean_pg)
    holder = psycopg2.connect(get_test_dsn())
    try:
        with holder.cursor() as cur:  # implicit BEGIN; ACCESS SHARE held until rollback
            cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
        body = (_MIGRATIONS_DIR / name).read_text()
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            with clean_pg.cursor() as cur:
                cur.execute(body)
        assert time.monotonic() - started < 30, "gave up within the lock timeout"
    finally:
        holder.rollback()
        holder.close()
