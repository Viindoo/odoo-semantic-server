# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for writer_pgvector — make_chunks (unit) + write/query (postgres integration)."""
import pytest

from src.indexer.embedder import FakeEmbedder
from src.indexer.models import (
    CSSChunk,
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
    _INSERT_SQL,
    EmbeddingChunk,
    _embed_chunks_resilient,
    make_chunks,
    make_pattern_chunks,
    write_module_embeddings,
)

TEST_VERSION = "99.0"
TEST_MODULE = "test_sale"


def _insert_columns() -> list[str]:
    """Parse the column list from the embeddings INSERT SQL (SSOT).

    The contract `as_tuple()` must satisfy is that its positional output is
    aligned, column-by-column, with this INSERT's column list. Deriving the
    names here ties the test to the real SQL rather than to a magic index.
    """
    inside = _INSERT_SQL.split("(", 1)[1].split(")", 1)[0]
    return [c.strip() for c in inside.split(",")]


def _as_dict(chunk: EmbeddingChunk, vec, **kw) -> dict:
    """Map as_tuple() output onto INSERT column names → {column: value}.

    Asserting against this dict tests the observable contract (the value that
    lands in each named DB column) instead of pinning a tuple index, so a
    field reorder that breaks column alignment is caught regardless of position.
    """
    cols = _insert_columns()
    values = chunk.as_tuple(vec, **kw)
    assert len(values) == len(cols), (
        f"as_tuple arity ({len(values)}) must match INSERT columns ({len(cols)}): {cols}"
    )
    return dict(zip(cols, values))


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
    """as_tuple must include profile_name (positioned before the A3 provenance fields)."""
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
        profile_name="tenant_acme",
    )
    row = _as_dict(chunk, [0.0] * 1024)
    assert row["chunk_type"] == "method"
    assert row["profile_name"] == "tenant_acme"


def test_embedding_chunk_as_tuple_profile_name_none():
    """as_tuple with profile_name=None writes NULL to the profile_name column."""
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
    )
    row = _as_dict(chunk, [0.0] * 1024)
    assert row["profile_name"] is None


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


# --- WI-A3: embeddings provenance (unit tests, no DB needed) ---


def test_make_chunks_method_file_path_is_real_file_not_module_dir():
    """Method chunk file_path must equal model.file_path, not the module directory."""
    mi = ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="my_repo", path="/repo/my_module", depends=[],
        repo_id=42,
    )
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[MethodInfo(name="action_confirm",
                            source_code="def action_confirm(self):\n    pass",
                            line=15)],
        file_path="/repo/my_module/models/sale_order.py",  # real file path (A3)
    )
    result = ParseResult(module=mi, models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert len(method_chunks) == 1
    assert method_chunks[0].file_path == "/repo/my_module/models/sale_order.py"
    assert method_chunks[0].file_path != "/repo/my_module"  # NOT the module dir


def test_make_chunks_method_line_start_propagated():
    """Method chunk line_start must equal method.line."""
    mi = ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="my_repo", path="/repo/my_module", depends=[],
    )
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[MethodInfo(name="action_confirm",
                            source_code="def action_confirm(self):\n    pass",
                            line=37)],
        file_path="/repo/my_module/models/sale_order.py",
    )
    result = ParseResult(module=mi, models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert method_chunks[0].line_start == 37


def test_make_chunks_method_repo_and_repo_id_propagated():
    """Method chunk repo / repo_id come from parse_result.module."""
    mi = ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="viindoo_17", path="/repo/my_module", depends=[],
        repo_id=7,
    )
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[MethodInfo(name="action_confirm",
                            source_code="def action_confirm(self):\n    pass")],
    )
    result = ParseResult(module=mi, models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert method_chunks[0].repo == "viindoo_17"
    assert method_chunks[0].repo_id == 7


def test_make_chunks_field_provenance():
    """Field chunk carries real file_path, line_start, repo, repo_id."""
    mi = ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="my_repo", path="/repo/my_module", depends=[],
        repo_id=5,
    )
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        fields=[FieldInfo(
            name="amount_total", ttype="monetary",
            source_definition="amount_total = fields.Monetary(compute='_compute_amount')",
            line=22,
        )],
        file_path="/repo/my_module/models/sale_order.py",
    )
    result = ParseResult(module=mi, models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    field_chunks = [c for c in chunks if c.chunk_type == "field"]
    assert len(field_chunks) == 1
    assert field_chunks[0].file_path == "/repo/my_module/models/sale_order.py"
    assert field_chunks[0].line_start == 22
    assert field_chunks[0].repo == "my_repo"
    assert field_chunks[0].repo_id == 5


def test_make_chunks_method_fallback_to_module_path_when_no_file_path():
    """When model.file_path is None, method chunk file_path falls back to module dir."""
    mi = ModuleInfo(
        name=TEST_MODULE, odoo_version=TEST_VERSION,
        repo="my_repo", path="/repo/my_module", depends=[],
    )
    model = ModelInfo(
        name="sale.order", module=TEST_MODULE, odoo_version=TEST_VERSION,
        methods=[MethodInfo(name="write", source_code="def write(self, vals): pass")],
        file_path=None,  # not set — fallback expected
    )
    result = ParseResult(module=mi, models=[model])
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, result, None, None)
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert method_chunks[0].file_path == "/repo/my_module"


def test_embedding_chunk_as_tuple_includes_provenance():
    """as_tuple routes each provenance value to its named INSERT column.

    The contract is that the provenance + embedding-meta fields land in the
    line_start / repo / repo_id / embedding_model / embedding_dim columns.
    Asserting by column name (not tuple index) catches a field reorder that
    would silently write repo into the line_start column — the failure mode
    the previous t[-5..-1] index-pinning crudely approximated.
    """
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.confirm",
        "sale.order", "/tmp/sale.py", 0, "def confirm(self): pass",
        profile_name="tenant_x",
        line_start=42,
        repo="viindoo_17",
        repo_id=3,
    )
    row = _as_dict(chunk, [0.0] * 1024, embedding_model="fake", embedding_dim=1024)
    assert row["profile_name"] == "tenant_x"
    assert row["line_start"] == 42
    assert row["repo"] == "viindoo_17"
    assert row["repo_id"] == 3
    assert row["embedding_model"] == "fake"
    assert row["embedding_dim"] == 1024


def test_embedding_chunk_provenance_defaults_to_none():
    """New provenance fields default to None for backward-compat."""
    chunk = EmbeddingChunk(
        "css", TEST_MODULE, TEST_VERSION, "my_selector",
        None, "/tmp/style.css", 0, ".foo { color: red }",
    )
    assert chunk.line_start is None
    assert chunk.repo is None
    assert chunk.repo_id is None


# --- WI-A3: migration columns (postgres integration) ---


@pytest.mark.postgres
def test_write_migration_adds_provenance_columns(clean_pg_embeddings):
    """After run_migrations, embeddings must have line_start, repo, repo_id columns."""
    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'embeddings' AND column_name IN ('line_start','repo','repo_id') "
            "ORDER BY column_name"
        )
        found = {r[0] for r in cur.fetchall()}
    assert "line_start" in found, "line_start column missing from embeddings table"
    assert "repo" in found, "repo column missing from embeddings table"
    assert "repo_id" in found, "repo_id column missing from embeddings table"


# --- T6 (V16-G2): JSPatch chunk entity_name must be target, not patch_name ---


def test_make_chunks_jspatch_entity_name_is_target():
    """V16-G2: JSChunk for a patch() call must have entity_name = target component name,
    not the patch_name string literal (which is used as the Neo4j key, not the component)."""
    mi = _module_info()
    # Simulate how parser_js._parse_era3 now emits entity_name = target ("FormController")
    # rather than patch_name ("mail").
    js = [JSChunk(
        module=TEST_MODULE, odoo_version=TEST_VERSION,
        file_path="/tmp/mail_form_controller_patch.js", era="era3",
        entity_name="FormController",  # target = the patched class
        chunk_idx=0,
        content="patch(FormController, { someMethod() { ... } });",
    )]
    chunks = make_chunks(TEST_MODULE, TEST_VERSION, ParseResult(module=mi), None, js)
    js_chunks = [c for c in chunks if c.chunk_type == "js_era3"]
    assert len(js_chunks) == 1
    assert js_chunks[0].entity_name == "FormController", (
        f"entity_name should be target 'FormController', got {js_chunks[0].entity_name!r} — "
        "V16-G2: patch chunk entity_name must be target, not patch_name"
    )


@pytest.mark.postgres
def test_write_provenance_columns_populated(clean_pg_embeddings):
    """write_module_embeddings stores line_start / repo / repo_id when set on chunks."""
    embedder = FakeEmbedder(dim=1024)
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "/tmp/sale.py", 0, "def action_confirm(self): pass",
        line_start=55,
        repo="viindoo_17",
        repo_id=9,
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk], embedder)

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT line_start, repo, repo_id FROM embeddings "
            "WHERE module = %s AND odoo_version = %s AND entity_name = %s",
            (TEST_MODULE, TEST_VERSION, "sale.order.action_confirm"),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 55
    assert row[1] == "viindoo_17"
    assert row[2] == 9


# --- WI-B: token-bounded chunking + resilient embed + embedding_model/dim ---


def test_make_pattern_chunks_large_pattern_splits_into_multiple_rows():
    """A pattern whose content exceeds EMBEDDER_TOKEN_BUDGET must produce multiple chunks.

    Business rule: no chunk sent to the embedder may exceed the token budget so
    the embedding model context window is never overflowed.  split_by_token_budget
    is the mechanism; the unique key (chunk_type, entity_name, file_path, chunk_idx)
    remains intact because chunk_idx increments per split piece.
    """
    from src.constants import EMBEDDER_CHARS_PER_TOKEN, EMBEDDER_TOKEN_BUDGET

    # Build a snippet large enough to exceed the budget.
    # chars_per_token=3.0 (EMBEDDER_CHARS_PER_TOKEN), so budget chars ≈ budget * 3.
    budget_chars = int(EMBEDDER_TOKEN_BUDGET * EMBEDDER_CHARS_PER_TOKEN)
    big_snippet = "x" * (budget_chars + 500)  # definitely over budget
    pattern = PatternExample(
        pattern_id="big-pattern",
        intent_keywords=["oversized"],
        file_ref="addons/sale/models/sale_order.py:1",
        snippet_text=big_snippet,
        gotchas=[],
        odoo_version_min="17.0",
        language="python",
    )
    chunks = make_pattern_chunks([pattern])
    # Must produce more than one chunk for the oversized pattern.
    assert len(chunks) > 1, (
        f"Oversized pattern should produce multiple chunks; got {len(chunks)}"
    )
    # All chunks share the same entity_name + module + version + file_path.
    entity_names = {c.entity_name for c in chunks}
    assert entity_names == {"python__big-pattern"}, (
        f"All split chunks must share entity_name; got {entity_names}"
    )
    # chunk_idx must be unique and start at 0.
    idxs = [c.chunk_idx for c in chunks]
    assert idxs == list(range(len(chunks))), (
        f"chunk_idx must be sequential starting at 0; got {idxs}"
    )
    # Each individual chunk must fit within budget.
    from src.indexer.embedder import estimate_tokens
    for c in chunks:
        tokens = estimate_tokens(c.content)
        assert tokens <= EMBEDDER_TOKEN_BUDGET, (
            f"Chunk {c.chunk_idx} exceeds token budget: {tokens} > {EMBEDDER_TOKEN_BUDGET}"
        )


def test_embed_chunks_resilient_skips_failing_chunk_and_logs(caplog):
    """_embed_chunks_resilient must skip chunks that fail per-chunk embed and log a warning.

    Business rule: a single bad chunk (e.g. model-rejected content) must not
    abort the entire batch. The surviving chunks and their vectors are returned
    aligned; the failing chunk is absent from the output and a warning is logged.
    """
    import logging

    # FakeEmbedder that raises for a specific content.
    class PickyEmbedder(FakeEmbedder):
        def embed(self, texts: list[str]) -> list[list[float]]:
            if len(texts) > 1:
                # Simulate batch failure to trigger per-chunk fallback.
                raise RuntimeError("batch too large for picky embedder")
            if texts[0] == "FAIL_ME":
                raise RuntimeError("content rejected by embedder")
            return super().embed(texts)

    embedder = PickyEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk("method", "mod", "17.0", "a", None, "/f.py", 0, "good content"),
        EmbeddingChunk("method", "mod", "17.0", "b", None, "/f.py", 1, "FAIL_ME"),
        EmbeddingChunk("method", "mod", "17.0", "c", None, "/f.py", 2, "also good"),
    ]

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_pgvector"):
        ok_chunks, vecs, embed_calls = _embed_chunks_resilient(embedder, chunks)

    # Only the 2 surviving chunks are returned.
    assert len(ok_chunks) == 2, f"Expected 2 surviving chunks, got {len(ok_chunks)}"
    assert len(vecs) == 2
    surviving_names = {c.entity_name for c in ok_chunks}
    assert "b" not in surviving_names, "Failing chunk 'b' must be absent from output"
    assert "a" in surviving_names
    assert "c" in surviving_names

    # A warning must be logged for the skipped chunk.
    skip_warnings = [
        r for r in caplog.records
        if "FAIL_ME" in r.message or "skipped" in r.message.lower()
    ]
    assert skip_warnings, (
        "Expected a warning log about the skipped chunk; got caplog records: "
        f"{[r.message for r in caplog.records]}"
    )


@pytest.mark.postgres
def test_write_embedding_model_dim_stored_on_rows(clean_pg_embeddings):
    """write_module_embeddings must store embedding_model and embedding_dim on each row.

    Business rule (ADR-0042 / WI-B): the writer stamps every inserted row with
    the model identifier and dimension so the fail-fast guard (assert_dim_matches)
    can detect incompatible vector spaces before mixing them.
    """
    embedder = FakeEmbedder(dim=1024, model="test-model-v1")
    chunk = EmbeddingChunk(
        "method", TEST_MODULE, TEST_VERSION, "sale.order.test_method",
        "sale.order", "/tmp/sale.py", 0, "def test_method(self): pass",
    )
    write_module_embeddings(TEST_MODULE, TEST_VERSION, [chunk], embedder)

    with clean_pg_embeddings.cursor() as cur:
        cur.execute(
            "SELECT embedding_model, embedding_dim FROM embeddings "
            "WHERE module = %s AND odoo_version = %s AND entity_name = %s",
            (TEST_MODULE, TEST_VERSION, "sale.order.test_method"),
        )
        row = cur.fetchone()

    assert row is not None, "Expected a row in embeddings after write"
    assert row[0] == "test-model-v1", (
        f"embedding_model should be 'test-model-v1', got {row[0]!r}"
    )
    assert row[1] == 1024, (
        f"embedding_dim should be 1024, got {row[1]!r}"
    )


# --- Fix #1: total embed failure guard (unit, no DB needed) ---


def test_embed_chunks_resilient_total_failure_returns_empty(caplog):
    """_embed_chunks_resilient must return empty lists when ALL chunks fail to embed.

    Business rule: total-failure path must NOT trigger a destructive DELETE of
    existing embeddings. The guard in write_module_embeddings checks
    ``not live_chunks`` and skips the DB write, but only if
    _embed_chunks_resilient correctly signals total failure via empty lists.
    """
    import logging

    class AlwaysFailEmbedder(FakeEmbedder):
        """Every embed() call raises — simulates a dead or misconfigured endpoint."""
        def embed(self, texts):
            raise RuntimeError("service unavailable")

    embedder = AlwaysFailEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk("method", "mod", "17.0", "a", None, "/f.py", 0, "good content"),
        EmbeddingChunk("method", "mod", "17.0", "b", None, "/f.py", 1, "other content"),
    ]

    with caplog.at_level(logging.WARNING, logger="src.indexer.writer_pgvector"):
        ok_chunks, vecs, embed_calls = _embed_chunks_resilient(embedder, chunks)

    assert ok_chunks == [], (
        f"Total failure must return empty chunk list; got {ok_chunks}"
    )
    assert vecs == [], (
        f"Total failure must return empty vec list; got {vecs}"
    )
    # Warnings must have been logged (batch fail + retry fail + per-chunk skips)
    assert caplog.records, "Expected warning logs for total embed failure"


def test_embed_chunks_resilient_retry_batch_before_degrade(caplog):
    """_embed_chunks_resilient must retry the full batch once before degrading per-chunk.

    Business rule (fix #7): a transient error should not immediately cause N
    per-chunk calls.  The implementation must attempt a full-batch retry; only
    if the retry also fails does it fall back to per-chunk mode.
    """
    import logging

    call_log: list[str] = []

    class TransientEmbedder(FakeEmbedder):
        """Fails on the first batch call, succeeds on the second (retry)."""
        def embed(self, texts):
            if len(texts) > 1:
                call_log.append(f"batch({len(texts)})")
                if call_log.count(f"batch({len(texts)})") == 1:
                    raise RuntimeError("transient error")
                # Second call (retry) succeeds
            else:
                call_log.append("single")
            return super().embed(texts)

    embedder = TransientEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk("method", "mod", "17.0", "a", None, "/f.py", 0, "content a"),
        EmbeddingChunk("method", "mod", "17.0", "b", None, "/f.py", 1, "content b"),
    ]

    with caplog.at_level(logging.INFO, logger="src.indexer.writer_pgvector"):
        ok_chunks, vecs, _ = _embed_chunks_resilient(embedder, chunks)

    # All chunks must survive because the retry succeeded
    assert len(ok_chunks) == 2, (
        f"Retry should have recovered all chunks; got {len(ok_chunks)}"
    )
    assert len(vecs) == 2
    # Verify that we went through the retry (2 batch calls) not per-chunk (many singles)
    batch_calls = [c for c in call_log if c.startswith("batch")]
    single_calls = [c for c in call_log if c == "single"]
    assert len(batch_calls) == 2, (
        f"Expected 2 batch attempts (initial + retry); got {batch_calls}"
    )
    assert single_calls == [], (
        f"Should not have fallen through to per-chunk calls; got {single_calls}"
    )


# --- Fix #3: chunk_idx uniqueness after token-split (unit, no DB needed) ---


def test_make_css_chunks_split_chunk_idx_unique():
    """make_css_chunks: split chunks for 2 css entities must have unique chunk_idx per entity.

    Business rule (fix #8): when a CSSChunk content is split by token budget,
    the resulting EmbeddingChunks must NOT collide on (entity_name, file_path,
    chunk_idx).  Specifically, chunk 0 of entity B must not shadow split piece 0
    of entity A when A was split into multiple pieces.
    """
    from src.constants import EMBEDDER_CHARS_PER_TOKEN, EMBEDDER_TOKEN_BUDGET
    from src.indexer.writer_pgvector import make_css_chunks

    budget_chars = int(EMBEDDER_TOKEN_BUDGET * EMBEDDER_CHARS_PER_TOKEN)
    # entity_a needs a split (> budget), entity_b fits in one chunk
    big_content = "x" * (budget_chars + 200)
    small_content = ".small { color: red }"

    css_chunks = [
        CSSChunk(
            module=TEST_MODULE, odoo_version=TEST_VERSION,
            file_path="/tmp/style.css", chunk_kind="raw", chunk_idx=0,
            entity_name="big-entity", content=big_content,
        ),
        CSSChunk(
            module=TEST_MODULE, odoo_version=TEST_VERSION,
            file_path="/tmp/style.css", chunk_kind="selector", chunk_idx=1,
            entity_name="small-entity", content=small_content,
        ),
    ]
    chunks = make_css_chunks(css_chunks)

    # All (entity_name, file_path, chunk_idx) tuples must be unique
    keys = [(c.entity_name, c.file_path, c.chunk_idx) for c in chunks]
    assert len(keys) == len(set(keys)), (
        f"chunk_idx collision detected in make_css_chunks output: "
        f"{[k for k in keys if keys.count(k) > 1]}"
    )
    # big-entity must have produced more than 1 chunk
    big_chunks = [c for c in chunks if c.entity_name == "big-entity"]
    assert len(big_chunks) > 1, (
        f"big-entity should have been split; got {len(big_chunks)} chunk(s)"
    )
    # small-entity must have exactly 1 chunk
    small_chunks = [c for c in chunks if c.entity_name == "small-entity"]
    assert len(small_chunks) == 1, (
        f"small-entity should not be split; got {len(small_chunks)} chunk(s)"
    )


def test_sliding_two_windows_one_split_chunk_idx_unique():
    """_sliding: 2 windows + 1 window split by token budget must not collide on chunk_idx.

    Business rule (fix #8): when a char-window is further split by
    split_by_token_budget, the resulting sub-chunks must use a globally
    monotonic chunk_idx so the (entity_name, file_path, chunk_idx) unique key
    is never violated within a single entity.
    """
    from src.constants import EMBEDDER_CHARS_PER_TOKEN, EMBEDDER_TOKEN_BUDGET
    from src.indexer.writer_pgvector import _WINDOW_CHARS, _sliding

    # Build a raw string that:
    # 1. Requires at least 2 char-windows (_WINDOW_CHARS)
    # 2. Has at least one window that exceeds EMBEDDER_TOKEN_BUDGET tokens
    budget_chars = int(EMBEDDER_TOKEN_BUDGET * EMBEDDER_CHARS_PER_TOKEN)
    # Make each window longer than the token budget by padding with 'a'
    # Raw length: 3 * _WINDOW_CHARS to ensure multiple windows
    raw = "a" * max(budget_chars + 200, _WINDOW_CHARS * 2 + 200)

    chunks = _sliding(
        raw, "test-entity", "method", TEST_MODULE, TEST_VERSION,
        "/tmp/f.py", None,
    )

    # All chunk_idx must be unique (no collisions)
    idxs = [c.chunk_idx for c in chunks]
    assert len(idxs) == len(set(idxs)), (
        f"chunk_idx collision in _sliding output: "
        f"{[i for i in idxs if idxs.count(i) > 1]}"
    )
    # Must have produced more than 1 chunk
    assert len(chunks) > 1, (
        f"Expected multiple chunks from large raw; got {len(chunks)}"
    )
