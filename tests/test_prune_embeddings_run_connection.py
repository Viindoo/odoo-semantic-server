# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entity prune deletes stale embeddings on the run's own database (F51).

Rule: when a module is re-parsed and loses an entity (B14 entity prune), the
entity's embedding rows are deleted on the connection the run was given
(``index_profile(pg_conn=...)``, autocommit) - the database every other
lifecycle write of the run lands on - whatever the process-wide PostgreSQL pool
happens to point at.

Real case (F51): the embedding reconcile always took a connection from the
process-global pool. In the test session that pool could point at another
database (a wiped schema -> ``UndefinedTable``, order-dependent); on a host
whose pool and run connection differ, the DELETE silently hit the wrong
database and the removed field kept its embedding in the run's own database
(the product rule "never blur data for the AI": ``find_examples`` keeps
returning an entity the source no longer has).

The test runs the prune with the run connection on database X while the
process pool points at a second, fully migrated database Y that knows the same
profile - so everything else the run does through the pool still works, and
only the database of the DELETE decides the outcome.
"""
from __future__ import annotations

import contextlib
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import psycopg2.extensions as pext
import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from tests import conftest
from tests._lifecycle_repo import GitRepo, V, register, run, write_module

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

M = "viin_ai_rag"
PROFILE = "viindoo_99"


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


@pytest.fixture
def other_db():
    """A second migrated database Y (dropped afterwards); yields its DSN."""
    admin_dsn = conftest._PG_ADMIN_DSN
    if not admin_dsn:
        pytest.skip("PG_ADMIN_DSN not set - a second database cannot be created")
    name = f"osm_test_f51_{uuid.uuid4().hex[:8]}"
    conftest._assert_pg_db_name_is_safe(name)
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {pext.quote_ident(name, admin)}")
    dsn = urlunsplit(urlsplit(admin_dsn)._replace(path=f"/{name}"))
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        run_migrations(conn)
        conn.close()
        yield dsn
    finally:
        conftest._drop_ephemeral_db(admin, name)
        admin.close()


@contextlib.contextmanager
def _process_pool_on(dsn: str):
    """Point the process-wide pool (and the stores cached on it) at *dsn*."""
    import src.db.pg as pg_mod

    saved = {n: getattr(pg_mod, n, None) for n in conftest._PG_POOL_GLOBALS}
    for n in conftest._PG_POOL_GLOBALS:
        setattr(pg_mod, n, None)
    pg_mod.init_pool(dsn, min_conn=1, max_conn=3)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            pg_mod.get_pool().close()
        for n, value in saved.items():
            setattr(pg_mod, n, value)


def _field_rows(conn, entity: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND profile_name = %s AND chunk_type = 'field' AND entity_name = %s",
            (V, M, PROFILE, entity),
        )
        return cur.fetchone()[0]


def test_removed_entity_embedding_is_deleted_in_the_run_database_not_the_pool_database(
    pg, neo4j_driver, tmp_path, other_db,
):
    """viin_ai_rag loses ``rag_note``. The second run is given its own autocommit
    connection to database X while the process pool points at database Y (same
    profile and repo registered there). X - the run's database - must lose the
    rag_note embedding and keep the rows of the entities the module still
    defines; the stale row may not survive in X because the DELETE went
    elsewhere."""
    repo = GitRepo(tmp_path, "viindoo_addons")
    write_module(repo, M, model="ai.rag.source", extra_field="rag_note = fields.Text()")
    repo.commit("add viin_ai_rag with rag_note")
    register(PROFILE, repo)
    run(pg, PROFILE)
    assert _field_rows(pg, "ai.rag.source.rag_note") == 1, "precondition: rag_note embedded in X"
    assert _field_rows(pg, "ai.rag.source.label") == 1

    write_module(repo, M, model="ai.rag.source")
    repo.commit(f"[REM] {M}: move rag_note out")
    with _process_pool_on(other_db):
        register(PROFILE, repo)
        summary = run(pg, PROFILE)

    assert summary["modules"] >= 1, summary
    with neo4j_driver.session() as s:
        left = s.run(
            "MATCH (f:Field {name: 'rag_note', module: $m, odoo_version: $v}) RETURN count(f) AS n",
            m=M, v=V,
        ).single()["n"]
    assert left == 0, "precondition: the graph prune removed rag_note"
    assert _field_rows(pg, "ai.rag.source.rag_note") == 0, (
        "the removed field's embedding survived in the run's own database"
    )
    assert _field_rows(pg, "ai.rag.source.label") == 1, "a still-defined entity lost its row"
