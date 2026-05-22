# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for writer_pgvector — make_chunks (unit) + write/query (postgres integration)."""
import pytest

from src.indexer.embedder import FakeEmbedder
from src.indexer.models import (
    FieldInfo,
    JSChunk,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
    PatternExample,
    QWebInfo,
    ViewInfo,
    ViewParseResult,
)
from src.indexer.writer_pgvector import (
    EmbeddingChunk,
    make_chunks,
    make_pattern_chunks,
    write_module_embeddings,
)

TEST_VERSION = "99.0"
TEST_MODULE = "test_sale"


def _module_info() -> ModuleInfo:
    return ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="test", path="/tmp/test_sale", depends=[],
    )


# --- make_chunks (no DB needed) ---

def test_make_chunks_from_method():
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[
            MethodInfo(name="action_confirm", source_code="def action_confirm(self):\n    pass")
        ],
    )
    result = ParseResult(module=_module_info(), models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert len(method_chunks) == 1
    assert "action_confirm" in method_chunks[0].entity_name
    assert "action_confirm" in method_chunks[0].content
    assert method_chunks[0].model_name == "sale.order"


def test_make_chunks_from_field():
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        fields=[FieldInfo(
            name="amount_total", ttype="monetary",
            source_definition="amount_total = fields.Monetary(compute='_compute_amount')",
        )],
    )
    result = ParseResult(module=_module_info(), models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    field_chunks = [c for c in chunks if c.chunk_type == "field"]
    assert len(field_chunks) == 1
    assert "amount_total" in field_chunks[0].entity_name
    assert field_chunks[0].model_name == "sale.order"


def test_make_chunks_from_view():
    mi = _module_info()
    view = ViewInfo(
        xmlid=f"{TEST_MODULE}.view_sale_order_form", name="Sale Order", model="sale.order",
        module=TEST_MODULE, odoo_version=TEST_VERSION, view_type="form", mode="primary",
        inherit_xmlid=None, arch="<form><field name='name'/></form>",
        file_path="/tmp/test_sale/views/sale_views.xml",
    )
    vr = ViewParseResult(module=mi, views=[view])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, ParseResult(module=mi), vr, None)
    view_chunks = [c for c in chunks if c.chunk_type == "view"]
    assert len(view_chunks) >= 1
    assert "view_sale_order_form" in view_chunks[0].entity_name


def test_make_chunks_from_qweb():
    mi = _module_info()
    qweb = QWebInfo(
        xmlid=f"{TEST_MODULE}.portal_sale_template", module=TEST_MODULE,
        odoo_version=TEST_VERSION, content="<template><t>hello</t></template>",
        file_path="/tmp/test_sale/views/portal.xml",
    )
    vr = ViewParseResult(module=mi, qweb=[qweb])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, ParseResult(module=mi), vr, None)
    qweb_chunks = [c for c in chunks if c.chunk_type == "qweb"]
    assert len(qweb_chunks) >= 1
    assert qweb_chunks[0].model_name is None


def test_make_chunks_from_js():
    mi = _module_info()
    js = [JSChunk(
        module=TEST_MODULE, odoo_version=TEST_VERSION,
        file_path="/tmp/w.js", era="era3", entity_name="MyWidget",
        chunk_idx=0, content="class MyWidget {}",
    )]
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, ParseResult(module=mi), None, js)
    js_chunks = [c for c in chunks if c.chunk_type == "js_era3"]
    assert len(js_chunks) == 1
    assert js_chunks[0].model_name is None


def test_make_chunks_method_no_source_uses_placeholder():
    model = ModelInfo(
        name="res.partner", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[MethodInfo(name="write")],  # no source_code
    )
    result = ParseResult(module=_module_info(), models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert len(method_chunks) == 1
    assert "write" in method_chunks[0].content


def test_make_chunks_large_view_sliding_window():
    mi = _module_info()
    big_arch = "<form>" + "<field name='x'/>" * 200 + "</form>"  # ~4000+ chars
    view = ViewInfo(
        xmlid=f"{TEST_MODULE}.big_view", name="Big", model="sale.order",
        module=TEST_MODULE, odoo_version=TEST_VERSION, view_type="form", mode="primary",
        inherit_xmlid=None, arch=big_arch, file_path="/tmp/test_sale/views/big.xml",
    )
    vr = ViewParseResult(module=mi, views=[view])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, ParseResult(module=mi), vr, None)
    view_chunks = [c for c in chunks if c.chunk_type == "view"]
    assert len(view_chunks) > 1


# --- make_pattern_chunks (M4.6 WI3, no DB needed) ---

def _sample_pattern() -> PatternExample:
    return PatternExample(
        pattern_id="computed-field-cross-model",
        intent_keywords=["computed", "depends"],
        file_ref="addons/sale/models/sale_order.py:245",
        snippet_text="@api.depends('partner_id.country_id')\ndef _compute(self): ...",
        gotchas=["Missing Many2one root in path"],
        odoo_version_min="17.0",
        language="python",
    )


def test_make_pattern_chunks_chunk_type():
    chunks = make_pattern_chunks([_sample_pattern()])
    assert len(chunks) == 1
    assert chunks[0].chunk_type == "pattern_example"


def test_make_pattern_chunks_module_sentinel():
    chunks = make_pattern_chunks([_sample_pattern()])
    assert chunks[0].module == "__patterns__"


def test_make_pattern_chunks_entity_name_slug():
    chunks = make_pattern_chunks([_sample_pattern()])
    assert chunks[0].entity_name == "python__computed-field-cross-model"


def test_make_pattern_chunks_text_includes_snippet_and_gotchas():
    chunks = make_pattern_chunks([_sample_pattern()])
    assert "_compute" in chunks[0].content
    assert "Many2one root" in chunks[0].content


def test_make_pattern_chunks_uses_version_min():
    chunks = make_pattern_chunks([_sample_pattern()])
    assert chunks[0].odoo_version == "17.0"
    assert chunks[0].file_path == "addons/sale/models/sale_order.py:245"


# --- write_module_embeddings (postgres integration) ---

pytestmark_pg = pytest.mark.postgres


@pytest.mark.postgres
def test_write_and_count_embeddings(clean_pg_embeddings):
    embedder = FakeEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk("method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
                       "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass"),
        EmbeddingChunk("field", TEST_MODULE, TEST_VERSION, "sale.order.amount_total",
                       "sale.order", "/tmp/sale.py", 0, "amount_total = fields.Monetary()"),
    ]
    write_module_embeddings(TEST_MODULE, TEST_VERSION, chunks, embedder)

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE module = %s AND odoo_version = %s",
            (TEST_MODULE, TEST_VERSION),
        )
        assert cur.fetchone()[0] == 2


@pytest.mark.postgres
def test_write_is_delete_before_insert(clean_pg_embeddings):
    embedder = FakeEmbedder(dim=1024)
    old_chunk = EmbeddingChunk("method", TEST_MODULE, TEST_VERSION, "sale.order.old",
                               "sale.order", "/tmp/sale.py", 0, "def old(self): pass")
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [old_chunk], embedder)

    new_chunk = EmbeddingChunk("method", TEST_MODULE, TEST_VERSION, "sale.order.new",
                               "sale.order", "/tmp/sale.py", 0, "def new(self): pass")
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [new_chunk], embedder)

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT entity_name FROM embeddings WHERE module = %s AND odoo_version = %s",
            (TEST_MODULE, TEST_VERSION),
        )
        names = [r[0] for r in cur.fetchall()]
    assert "sale.order.new" in names
    assert "sale.order.old" not in names


@pytest.mark.postgres
def test_ann_query_returns_nearest_result(clean_pg_embeddings):
    embedder = FakeEmbedder(dim=1024)
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "/tmp/sale.py", 0, "def action_confirm(self): ...",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk], embedder)

    query_vec = embedder.embed(["confirm sale order"])[0]
    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT entity_name FROM embeddings "
            "WHERE odoo_version = %s ORDER BY vec <=> %s::vector LIMIT 1",
            (TEST_VERSION, query_vec),
        )
        row = cur.fetchone()
    assert row is not None
    assert "action_confirm" in row[0]


# --- WI-B: profile_name column (unit tests, no DB needed) ---


def test_embedding_chunk_default_profile_name_is_none():
    """EmbeddingChunk.profile_name defaults to None (shared/global)."""
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
    )
    assert chunk.profile_name is None


def test_embedding_chunk_as_tuple_includes_profile_name():
    """as_tuple must include profile_name as the last element."""
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
        profile_name="tenant_acme",
    )
    vec = [0.0] * 1024
    t = chunk.as_tuple(vec)
    # tuple order: chunk_type, module, odoo_version, entity_name, model_name,
    #              file_path, chunk_idx, content, vec, profile_name
    assert t[-1] == "tenant_acme"
    assert t[0] == "method"


def test_embedding_chunk_as_tuple_profile_name_none():
    """as_tuple with profile_name=None passes None (global rows)."""
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
    )
    t = chunk.as_tuple([0.0] * 1024)
    assert t[-1] is None


def test_make_pattern_chunks_profile_name_is_none():
    """Pattern chunks must default to profile_name=None (shared/global)."""
    chunks = make_pattern_chunks([_sample_pattern()])
    assert chunks[0].profile_name is None


# --- WI-B: profile-scoped delete (postgres integration) ---


@pytest.mark.postgres
def test_write_stamps_profile_name_on_rows(clean_pg_embeddings):
    """write_module_embeddings stamps profile_name onto all written rows."""
    embedder = FakeEmbedder(dim=1024)
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk], embedder,
                            profile_name="profile_a")

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT profile_name FROM embeddings WHERE module = %s AND odoo_version = %s",
            (TEST_MODULE, TEST_VERSION),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "profile_a"


@pytest.mark.postgres
def test_write_profile_none_stamps_null(clean_pg_embeddings):
    """write_module_embeddings with no profile_name stores NULL."""
    embedder = FakeEmbedder(dim=1024)
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk], embedder)

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT profile_name FROM embeddings WHERE module = %s AND odoo_version = %s",
            (TEST_MODULE, TEST_VERSION),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None


@pytest.mark.postgres
def test_delete_is_profile_scoped_does_not_erase_other_profile(clean_pg_embeddings):
    """Re-indexing profile A must not delete profile B's chunks for the same module/version."""
    embedder = FakeEmbedder(dim=1024)

    # Write profile_a chunk
    chunk_a = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.action_a",
        "sale.order", "/tmp/sale.py", 0, "def action_a(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk_a], embedder,
                            profile_name="profile_a")

    # Write profile_b chunk for the same module/version
    chunk_b = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.action_b",
        "sale.order", "/tmp/sale.py", 1, "def action_b(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk_b], embedder,
                            profile_name="profile_b")

    # Re-index profile_a — must not touch profile_b rows
    chunk_a2 = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.action_a_v2",
        "sale.order", "/tmp/sale.py", 0, "def action_a_v2(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk_a2], embedder,
                            profile_name="profile_a")

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT entity_name, profile_name FROM embeddings "
            "WHERE module = %s AND odoo_version = %s ORDER BY entity_name",
            (TEST_MODULE, TEST_VERSION),
        )
        rows = {r[1]: r[0] for r in cur.fetchall()}

    # profile_a should now have action_a_v2 (old action_a was deleted by re-index)
    assert rows.get("profile_a") == "sale.order.action_a_v2"
    # profile_b must be untouched
    assert rows.get("profile_b") == "sale.order.action_b"


@pytest.mark.postgres
def test_write_migration_adds_profile_name_column(clean_pg_embeddings):
    """After run_migrations, the embeddings table must have a profile_name column."""
    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'embeddings' AND column_name = 'profile_name'"
        )
        row = cur.fetchone()
    assert row is not None, "profile_name column missing from embeddings table"
