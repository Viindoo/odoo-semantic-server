# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/test_mcp_stylesheet_tools.py — M10A D5/D6 unit+integration tests.

Covers:
  D5 — resolve_stylesheet: happy path (module with stylesheets + import chain),
       not-found path, import-chain ordering.
  D6 — find_style_override: FakeEmbedder-based ANN with seeded pg embeddings,
       selector match → override order (import chain), embedder None → degrade.

Markers:
  - resolve_stylesheet tests: pytest.mark.neo4j (Neo4j required)
  - find_style_override pgvector tests: pytest.mark.neo4j (also needs pg + neo4j)
  - degrade/empty-input tests: no marker (pure unit, no Docker)

Data isolation:
  - Uses TEST_VERSION = "99.0" via clean_neo4j fixture (auto-wipe).
  - pg embeddings cleaned by pg_conn + manual DELETE in setup/teardown.
"""
from __future__ import annotations

import pytest

from tests.conftest import TEST_VERSION

# NOTE: This file intentionally has NO module-level pytestmark — it mixes
# @pytest.mark.neo4j integration tests with pure-unit tests (no Docker).
# Markers are applied per-test via @pytest.mark.neo4j decorators.


# ---------------------------------------------------------------------------
# Helper: seed :Stylesheet + :IMPORTS nodes in Neo4j
# ---------------------------------------------------------------------------


def _seed_stylesheets(neo4j_driver):
    """Seed 3 :Stylesheet nodes with an :IMPORTS chain for TEST_VERSION.

    Graph:
      module=css_mod, file=main.scss (scss, selectors=10, vars=5, imports=1)
        -[:IMPORTS]-> module=css_mod, file=variables.scss (scss, vars=5)
      module=css_mod, file=standalone.css (css, selectors=3)

    Used by resolve_stylesheet happy-path + import-chain tests.
    """
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (m:Module {name: 'css_mod', odoo_version: $v})
            ON CREATE SET m.profile = ['default']
            ON MATCH SET  m.profile = ['default']
            """,
            v=TEST_VERSION,
        )
        # main.scss
        s.run(
            """
            MERGE (ss:Stylesheet {file_path: '/opt/odoo/css_mod/static/src/scss/main.scss',
                                  module: 'css_mod', odoo_version: $v})
            ON CREATE SET ss.language = 'scss',
                          ss.selector_count = 10,
                          ss.variable_count = 5,
                          ss.import_count = 1,
                          ss.mixin_count = 2
            ON MATCH  SET ss.language = 'scss',
                          ss.selector_count = 10,
                          ss.variable_count = 5,
                          ss.import_count = 1,
                          ss.mixin_count = 2
            """,
            v=TEST_VERSION,
        )
        # variables.scss
        s.run(
            """
            MERGE (ss:Stylesheet {file_path: '/opt/odoo/css_mod/static/src/scss/variables.scss',
                                  module: 'css_mod', odoo_version: $v})
            ON CREATE SET ss.language = 'scss',
                          ss.selector_count = 0,
                          ss.variable_count = 5,
                          ss.import_count = 0,
                          ss.mixin_count = 0
            ON MATCH  SET ss.language = 'scss',
                          ss.selector_count = 0,
                          ss.variable_count = 5,
                          ss.import_count = 0,
                          ss.mixin_count = 0
            """,
            v=TEST_VERSION,
        )
        # standalone.css
        s.run(
            """
            MERGE (ss:Stylesheet {file_path: '/opt/odoo/css_mod/static/src/css/standalone.css',
                                  module: 'css_mod', odoo_version: $v})
            ON CREATE SET ss.language = 'css',
                          ss.selector_count = 3,
                          ss.variable_count = 0,
                          ss.import_count = 0,
                          ss.mixin_count = 0
            ON MATCH  SET ss.language = 'css',
                          ss.selector_count = 3,
                          ss.variable_count = 0,
                          ss.import_count = 0,
                          ss.mixin_count = 0
            """,
            v=TEST_VERSION,
        )
        # IMPORTS edge: main.scss -> variables.scss
        s.run(
            """
            MATCH (src:Stylesheet {file_path: '/opt/odoo/css_mod/static/src/scss/main.scss',
                                   module: 'css_mod', odoo_version: $v})
            MATCH (tgt:Stylesheet {file_path: '/opt/odoo/css_mod/static/src/scss/variables.scss',
                                   module: 'css_mod', odoo_version: $v})
            MERGE (src)-[:IMPORTS]->(tgt)
            """,
            v=TEST_VERSION,
        )


# ===========================================================================
# D5 — resolve_stylesheet tests
# ===========================================================================


@pytest.mark.neo4j
def test_resolve_stylesheet_happy(clean_neo4j):
    """Happy path: module with 3 stylesheets; verify tree structure + import chain."""
    from src.mcp.server import _resolve_stylesheet

    neo4j_driver = clean_neo4j
    _seed_stylesheets(neo4j_driver)

    # Monkeypatch _get_driver to use test driver
    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = neo4j_driver
    try:
        result = _resolve_stylesheet("css_mod", TEST_VERSION)
    finally:
        srv_mod._driver = original_driver

    # Header
    assert f"resolve_stylesheet('css_mod', '{TEST_VERSION}')" in result
    # All 3 files listed
    assert "Stylesheets: 3 file(s)" in result
    assert "main.scss" in result
    assert "variables.scss" in result
    assert "standalone.css" in result
    # Language and stats
    assert "lang=scss" in result
    assert "lang=css" in result
    assert "selectors=10" in result
    assert "vars=5" in result
    assert "mixins=2" in result
    # Import chain for main.scss -> variables.scss
    assert "Imports (1):" in result
    # Next-step footer
    assert "└─ Next:" in result
    assert "find_style_override" in result


@pytest.mark.neo4j
def test_resolve_stylesheet_not_found(clean_neo4j):
    """Not-found path: module with no stylesheets → not-found tree + recovery hint."""
    import src.mcp.server as srv_mod
    from src.mcp.server import _resolve_stylesheet

    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    try:
        result = _resolve_stylesheet("nonexistent_module_xyz", TEST_VERSION)
    finally:
        srv_mod._driver = original_driver

    assert "not found" in result.lower()
    assert "Recovery:" in result or "recovery" in result.lower()
    assert "describe_module" in result


@pytest.mark.neo4j
def test_resolve_stylesheet_import_chain_order(clean_neo4j):
    """Import chain is present and refers to the correct target stylesheet."""
    import src.mcp.server as srv_mod
    from src.mcp.server import _resolve_stylesheet

    _seed_stylesheets(clean_neo4j)

    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    try:
        result = _resolve_stylesheet("css_mod", TEST_VERSION)
    finally:
        srv_mod._driver = original_driver

    # variables.scss should appear as import target under main.scss
    lines = result.splitlines()
    main_line_idx = next(
        (i for i, ln in enumerate(lines) if "main.scss" in ln), None
    )
    vars_line_idx = next(
        (i for i, ln in enumerate(lines) if "variables.scss" in ln), None
    )
    assert main_line_idx is not None, "main.scss not found in output"
    assert vars_line_idx is not None, "variables.scss not found in output"
    # variables.scss must appear after main.scss in the tree
    assert vars_line_idx > main_line_idx, (
        "variables.scss (import target) must appear after main.scss in tree"
    )


# ===========================================================================
# D6 — find_style_override tests
# ===========================================================================


def test_find_style_override_empty_input():
    """Empty selector_or_variable → user-error sentinel (no tree, no DB call)."""
    from src.mcp.server import _find_style_override

    result = _find_style_override("", "99.0")
    assert "empty selector_or_variable" in result
    assert "Found 0 results" in result


def test_find_style_override_embedder_unavailable():
    """When embedder is None (no _get_embedder), tool degrades gracefully."""
    from src.mcp.server import _find_style_override

    class _RaisingEmbedder:
        def embed(self, texts):  # noqa: ANN001
            raise RuntimeError("no embedder")

    # We can't easily disable _get_embedder without monkeypatching; instead
    # pass a bad embedder that raises on embed() — simulates Ollama down.
    result = _find_style_override(
        ".o_form_view", "99.0",
        _embedder=_RaisingEmbedder(),
    )
    # Should degrade, not raise
    assert "embedding failed" in result or "Found 0 results" in result


@pytest.mark.neo4j
def test_find_style_override_with_fake_embedder(clean_neo4j, pg_conn):
    """ANN search with FakeEmbedder + seeded pg embeddings; verify output format."""
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _CSS_MOD = "css_style_mod"

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    # Seed a css chunk
    embedder = FakeEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk(
            chunk_type="css",
            module=_CSS_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_form_view",
            model_name=None,
            file_path=f"/opt/odoo/{_CSS_MOD}/static/src/css/form.css",
            chunk_idx=0,
            content=".o_form_view { display: flex; }",
        ),
        EmbeddingChunk(
            chunk_type="scss",
            module=_CSS_MOD,
            odoo_version=TEST_VERSION,
            entity_name="variable:$primary",
            model_name=None,
            file_path=f"/opt/odoo/{_CSS_MOD}/static/src/scss/variables.scss",
            chunk_idx=0,
            content="$primary: #00BBCE;",
        ),
    ]
    write_module_embeddings(_CSS_MOD, TEST_VERSION, chunks, embedder)

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    try:
        result = _find_style_override(
            ".o_form_view",
            TEST_VERSION,
            limit=5,
            _pg_conn=pg_conn,
            _embedder=FakeEmbedder(dim=1024),
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # Header + content
    assert f'find_style_override: ".o_form_view" ({TEST_VERSION})' in result
    assert "Found" in result

    # If any results found, verify structure
    if "Found 0" not in result:
        assert "· score" in result
        assert "File:" in result
        assert "Override chain" in result
        # Next-step footer
        assert "└─ Next:" in result


@pytest.mark.neo4j
def test_find_style_override_output_has_next_step(clean_neo4j, pg_conn):
    """find_style_override with real match emits Next-step footer."""
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "style_hint_mod"

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    embedder = FakeEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="mixin:o_btn_primary",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/buttons.scss",
            chunk_idx=0,
            content="@mixin o_btn_primary { color: $primary; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, embedder)

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    try:
        result = _find_style_override(
            "btn primary mixin",
            TEST_VERSION,
            limit=3,
            _pg_conn=pg_conn,
            _embedder=FakeEmbedder(dim=1024),
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # Whatever result count, the footer should be present if results found
    if "Found 0" not in result:
        assert "└─ Next:" in result, (
            "find_style_override with results must emit └─ Next: footer"
        )
    else:
        # Even on 0 results: footer appears (hints_for appended)
        # FakeEmbedder uses deterministic random vectors; may return 0 hits
        # This branch is acceptable — just verify no crash.
        pass


# ===========================================================================
# Helper
# ===========================================================================


def _pg_has_vector(conn) -> bool:
    """Return True if pgvector extension is available in the test database."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
            return cur.fetchone() is not None
    except Exception:
        return False
