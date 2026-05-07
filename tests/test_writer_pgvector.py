"""Tests for writer_pgvector — make_chunks (unit) + write/query (postgres integration)."""
import pytest

from src.indexer.embedder import FakeEmbedder
from src.indexer.models import (
    FieldInfo, JSChunk, MethodInfo, ModelInfo, ModuleInfo,
    ParseResult, QWebInfo, ViewInfo, ViewParseResult,
)
from src.indexer.writer_pgvector import EmbeddingChunk, make_chunks, write_module_embeddings

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
        methods=[MethodInfo(name="action_confirm", source_code="def action_confirm(self):\n    pass")],
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
    write_module_embeddings(clean_pg_embeddings, TEST_MODULE, TEST_VERSION, chunks, embedder)

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
    write_module_embeddings(clean_pg_embeddings, TEST_MODULE, TEST_VERSION, [old_chunk], embedder)

    new_chunk = EmbeddingChunk("method", TEST_MODULE, TEST_VERSION, "sale.order.new",
                               "sale.order", "/tmp/sale.py", 0, "def new(self): pass")
    write_module_embeddings(clean_pg_embeddings, TEST_MODULE, TEST_VERSION, [new_chunk], embedder)

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
    write_module_embeddings(clean_pg_embeddings, TEST_MODULE, TEST_VERSION, [chunk], embedder)

    query_vec = embedder.embed(["confirm sale order"])[0]
    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT entity_name FROM embeddings WHERE odoo_version = %s ORDER BY vec <=> %s LIMIT 1",
            (TEST_VERSION, query_vec),
        )
        row = cur.fetchone()
    assert row is not None
    assert "action_confirm" in row[0]
