# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retirement's embedding deletes work when the table owner is subject to FORCEd RLS
(F28, ADR-0034, ``ops/rls_cutover.sh`` step 2).

Real case: a managed / split-tier Postgres where the owner DSN the indexer and
Web UI use is neither superuser nor BYPASSRLS. After the cutover FORCEs RLS on
``embeddings``, a plain ``DELETE`` by that owner sees only ``__global__`` rows
and removes nothing, so retired modules kept ghost embeddings silently.

Rules protected, on a scratch database owned by a ``NOSUPERUSER NOBYPASSRLS``
role with FORCE ROW LEVEL SECURITY on:

* ``delete_module_embeddings`` removes exactly the rows of the given profiles
  (other profiles, other modules and the global catalogue stay).
* ``embedding_groups`` (the orphan embedding sweep's read) sees every profile's groups.
* A delete that removes fewer rows than a prior read reported logs a WARNING.
* The unrestricted scope is transaction-local: it is gone after the call.
"""
from __future__ import annotations

import logging
import os
import uuid

import psycopg2
import psycopg2.extensions as pext
import pytest

from src.constants import GLOBAL_PROFILE
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres

V = "99.0"
RAG = "viin_ai_rag"


def _dsn(base: str, **over) -> str:
    parts = pext.parse_dsn(base)
    parts.update(over)
    return " ".join(f"{k}={v}" for k, v in parts.items())


@pytest.fixture
def owner_conn():
    """A connection as the non-bypass owner of a FORCEd-RLS ``embeddings`` table."""
    admin_dsn = os.getenv("PG_ADMIN_DSN")
    if not admin_dsn:
        pytest.skip("PG_ADMIN_DSN not set")
    tag = uuid.uuid4().hex[:8]
    db, role, password = f"osm_test_rls_{tag}", f"osm_rls_owner_{tag}", f"pw-{tag}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cur.fetchone()[0]:
            admin.close()
            pytest.skip("PG_ADMIN_DSN is not a superuser (needed for CREATE ROLE/DATABASE)")
        cur.execute(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %s',
                    (password,))
        cur.execute(f'CREATE DATABASE "{db}"')
    scratch = psycopg2.connect(_dsn(admin_dsn, dbname=db))
    scratch.autocommit = True
    owner = None
    try:
        with scratch.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        run_migrations(scratch)
        from pgvector.psycopg2 import register_vector
        register_vector(scratch)
        _seed(scratch)
        with scratch.cursor() as cur:
            cur.execute(f'ALTER TABLE embeddings OWNER TO "{role}"')
            cur.execute("ALTER TABLE embeddings FORCE ROW LEVEL SECURITY")
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        owner = psycopg2.connect(_dsn(admin_dsn, dbname=db, user=role, password=password))
        owner.autocommit = True
        with owner.cursor() as cur:
            cur.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            assert cur.fetchone() == (False, False), "positive control: a non-bypass owner"
        yield owner
    finally:
        if owner is not None:
            owner.close()
        scratch.close()
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (db,))
            cur.execute(f'DROP DATABASE IF EXISTS "{db}"')
            cur.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def _seed(conn) -> None:
    import numpy as np

    rows = [
        (RAG, "viindoo_99", "a"), (RAG, "viindoo_99", "b"), (RAG, "tvtma_99", "a"),
        ("viin_ai", "viindoo_99", "a"), ("__patterns__", GLOBAL_PROFILE, "p"),
    ]
    with conn.cursor() as cur:
        for module, profile, entity in rows:
            cur.execute(
                "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
                "file_path, chunk_idx, content, vec, profile_name) "
                "VALUES (%s, %s, %s, %s, %s, 0, 'x', %s, %s)",
                ("pattern_example" if profile == GLOBAL_PROFILE else "method",
                 module, V, f"{module}.{entity}", f"{module}/x.py",
                 np.zeros(1024, dtype=np.float32), profile),
            )


def _groups(owner) -> set[tuple[str, str]]:
    with owner.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL app.allowed_profiles = '*'")
        cur.execute("SELECT DISTINCT module, profile_name FROM embeddings")
        rows = set(cur.fetchall())
        cur.execute("COMMIT")
    return rows


def test_a_plain_owner_delete_is_blind_under_forced_rls(owner_conn):
    """Positive control for the scenario itself: without a scope the owner's
    DELETE matches nothing, so the fix below is what makes the rows go."""
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE module = %s", (RAG,))
        assert cur.rowcount == 0


def test_retiring_embeddings_under_forced_rls_deletes_exactly_the_given_profiles(owner_conn):
    from src.indexer.writer_pgvector import delete_module_embeddings

    before = _groups(owner_conn)
    deleted = delete_module_embeddings(owner_conn, RAG, V, ["viindoo_99"])

    assert deleted == 2
    assert _groups(owner_conn) == before - {(RAG, "viindoo_99")}


def test_orphan_scan_under_forced_rls_sees_every_profile(owner_conn):
    from src.indexer.writer_pgvector import embedding_groups

    assert embedding_groups(owner_conn, V) == [
        ("viin_ai", "viindoo_99", 1), (RAG, "tvtma_99", 1), (RAG, "viindoo_99", 2),
    ]


def test_a_short_delete_is_reported_as_a_warning(owner_conn, caplog):
    from src.indexer.writer_pgvector import delete_module_embeddings

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_pgvector"):
        deleted = delete_module_embeddings(owner_conn, RAG, V, ["tvtma_99"], expected=3)

    assert deleted == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1" in warnings[0].getMessage() and "3" in warnings[0].getMessage()


def test_a_complete_delete_is_not_reported(owner_conn, caplog):
    from src.indexer.writer_pgvector import delete_module_embeddings

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_pgvector"):
        assert delete_module_embeddings(owner_conn, RAG, V, ["viindoo_99"], expected=2) == 2
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_unrestricted_scope_does_not_outlive_the_call(owner_conn):
    # GUARD: safety bound of the fix (before it no scope was ever set).
    from src.indexer.writer_pgvector import delete_module_embeddings, embedding_groups

    delete_module_embeddings(owner_conn, RAG, V, ["viindoo_99"])
    embedding_groups(owner_conn, V)
    with owner_conn.cursor() as cur:
        cur.execute("SELECT coalesce(current_setting('app.allowed_profiles', true), '')")
        assert cur.fetchone()[0] == ""
        cur.execute("SELECT count(*) FROM embeddings")
        assert cur.fetchone()[0] == 1  # only the always-visible global catalogue row
