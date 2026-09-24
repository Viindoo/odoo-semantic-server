# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module retirement - Postgres side (ADR-0056 B6).

Business rules protected:

* Retiring a module deletes its pgvector embeddings for the retiring profiles
  ONLY: another tenant's chunks for the same module name, other modules, other
  versions and the global pattern catalogue survive (M6, ADR-0034).
* ``orphan_embedding_keys`` reports embedding groups no live owner accounts for,
  never the global catalogue.
* M4: after a module moves from repo A (profile A) to repo B (profile B),
  ``drop_module_owner`` makes the module's models and fields visible to a tenant
  scoped ONLY to B through the real read-side scope predicate, and A's tenant no
  longer sees it. Real case: ``to_saas_base`` moved between repos in one night.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from src.constants import GLOBAL_PROFILE
from src.indexer.embedder import FakeEmbedder
from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
from tests.conftest import PG_EMBED_VERSION as V

pytestmark = pytest.mark.postgres

RAG = "viin_ai_rag"


PATTERNS = "__patterns__"  # the global catalogue's module sentinel (GLOBAL_PROFILE rows)


def _chunk(module: str, entity: str, version: str = V, profile: str | None = None):
    return EmbeddingChunk(
        "pattern_example" if module == PATTERNS else "method",
        module, version, entity, "ai.rag.source",
        f"{module}/models/ai_rag_source.py", 0, f"def {entity.split('.')[-1]}(self): pass",
        profile_name=profile,
    )


def _write(module: str, profile: str, entity: str, version: str = V) -> None:
    write_module_embeddings(
        module, version, [_chunk(module, entity, version)], FakeEmbedder(dim=1024),
        profile_name=profile,
    )


def _rows(conn) -> set[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT module, odoo_version, profile_name FROM embeddings "
            "WHERE odoo_version IN (%s, %s)",
            (V, "98.0"),
        )
        return {tuple(r) for r in cur.fetchall()}


@pytest.fixture
def emb(clean_pg_embeddings):
    conn = clean_pg_embeddings
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = '98.0'")
    _write(RAG, "viindoo_99", "ai.rag.source.action_reindex")
    _write(RAG, "tvtma_99", "ai.rag.source.action_reindex_tvtma")
    _write("viin_ai", "viindoo_99", "ai.assistant.action_ask")
    _write(RAG, "viindoo_98", "ai.rag.source.action_reindex", version="98.0")
    _write(PATTERNS, GLOBAL_PROFILE, "computed-field-depends")
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = '98.0'")


def test_retiring_embeddings_deletes_only_the_retiring_profiles_rows(emb):
    """viin_ai_rag retired from viindoo_99: tvtma_99's chunks for the same name, the
    surviving viin_ai, the 98.0 copy and the global catalogue stay."""
    from src.indexer.writer_pgvector import delete_module_embeddings

    before = _rows(emb)

    deleted = delete_module_embeddings(emb, RAG, V, ["viindoo_99"])

    assert deleted == 1
    assert _rows(emb) == before - {(RAG, V, "viindoo_99")}


def test_retiring_embeddings_with_no_profile_deletes_nothing(emb):
    """An empty owner list must never widen into an unscoped delete."""
    from src.indexer.writer_pgvector import delete_module_embeddings

    before = _rows(emb)
    assert delete_module_embeddings(emb, RAG, V, []) == 0
    assert _rows(emb) == before


def test_retiring_embeddings_never_touches_the_global_catalogue(emb):
    """The pattern catalogue (GLOBAL_PROFILE) is not a tenant owner and cannot be
    retired through an owner list - not even by naming its own module sentinel."""
    from src.indexer.writer_pgvector import delete_module_embeddings

    assert delete_module_embeddings(emb, PATTERNS, V, [GLOBAL_PROFILE]) == 0
    deleted = delete_module_embeddings(emb, RAG, V, [GLOBAL_PROFILE, "tvtma_99"])

    assert deleted == 1
    rows = _rows(emb)
    assert (PATTERNS, V, GLOBAL_PROFILE) in rows
    assert (RAG, V, "tvtma_99") not in rows
    assert (RAG, V, "viindoo_99") in rows


def test_orphan_embedding_keys_reports_unowned_groups_only(emb):
    """Groups at the version with no live (module, profile) owner are reported,
    sorted by module then profile; live groups, other versions and the global
    catalogue are not."""
    from src.indexer.writer_pgvector import orphan_embedding_keys

    live = [("viin_ai", "viindoo_99"), (RAG, "viindoo_99")]

    assert orphan_embedding_keys(emb, V, live) == [(RAG, "tvtma_99", 1)]
    assert orphan_embedding_keys(emb, V, live + [(RAG, "tvtma_99")]) == []
    assert orphan_embedding_keys(emb, V, []) == [
        ("viin_ai", "viindoo_99", 1), (RAG, "tvtma_99", 1), (RAG, "viindoo_99", 1),
    ]


# ---------------------------------------------------------------------------
# M4 - A->B move: the B-only tenant sees the module through the real scope choke
# ---------------------------------------------------------------------------

_PFX = "rt_"


@contextmanager
def _as_tenant(tenant_id):
    from src.mcp import session
    from src.mcp.server import _tenant_id_var

    session.invalidate_allowed_profiles()
    token = _tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id_var.reset(token)
        session.invalidate_allowed_profiles()


def _cleanup_tenants(pg):
    with pg.cursor() as cur:
        cur.execute(rf"DELETE FROM profiles WHERE name LIKE '{_PFX}%%'")
        cur.execute(rf"DELETE FROM tenants  WHERE name LIKE '{_PFX}%%'")
    pg.commit()


@pytest.fixture
def moved_between_tenants(clean_pg_embeddings, clean_neo4j, tmp_path, monkeypatch):
    """Tenant A owns profile rt_a (repo A), tenant B owns rt_b (repo B); both repos
    have been indexed with viin_ai_rag, i.e. the module is mid-move A -> B."""
    from src.indexer.writer_neo4j import Neo4jWriter
    from tests import _retirement_fixture as fx

    pg = clean_pg_embeddings
    _cleanup_tenants(pg)
    with pg.cursor() as cur:
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}a') RETURNING id")
        tenant_a = cur.fetchone()[0]
        cur.execute(f"INSERT INTO tenants (name) VALUES ('{_PFX}b') RETURNING id")
        tenant_b = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, tenant_id) VALUES (%s, %s, %s), "
            "(%s, %s, %s)",
            (f"{_PFX}a", V, tenant_a, f"{_PFX}b", V, tenant_b),
        )
    pg.commit()

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    fx.build_and_index(tmp_path, writer, monkeypatch, repo_name="to_saas_repo_a",
                       profile=f"{_PFX}a", repo_id=7821)
    fx.build_and_index(tmp_path, writer, monkeypatch, repo_name="to_saas_repo_b",
                       profile=f"{_PFX}b", repo_id=7822)
    yield {"writer": writer, "a": tenant_a, "b": tenant_b}
    writer.close()
    _cleanup_tenants(pg)


def _sees_model_and_field(model: str, field: str) -> bool:
    from src.mcp.server import _resolve_field, _resolve_model

    m = _resolve_model(model, V)
    f = _resolve_field(model, field, V)
    model_ok = "not found" not in m.lower() and model in m
    field_ok = "not found" not in f.lower()
    return model_ok and field_ok


def test_after_move_b_only_tenant_sees_module_models_and_fields(moved_between_tenants):
    """M4: once A stops shipping viin_ai_rag, drop_module_owner(owners=[B]) must leave
    no A entry on any node, so a tenant scoped only to B sees ai.rag.source and its
    fields, and A's tenant no longer does."""
    from src.indexer.models import ModuleOwner

    t = moved_between_tenants
    with _as_tenant(t["b"]):
        assert not _sees_model_and_field("ai.rag.source", "name"), (
            "precondition: with both owners stamped, the fail-closed choke hides the "
            "module from a B-only tenant - this is the defect M4 closes"
        )

    t["writer"].drop_module_owner(
        V, RAG,
        [ModuleOwner(profile_name=f"{_PFX}b", repo_basename="to_saas_repo_b",
                     path=RAG, repo_id=7822)],
    )

    with _as_tenant(t["b"]):
        assert _sees_model_and_field("ai.rag.source", "name")
        assert _sees_model_and_field("ai.rag.source", "assistant_id")
    with _as_tenant(t["a"]):
        assert not _sees_model_and_field("ai.rag.source", "name"), (
            "A's tenant must no longer own the moved module"
        )
