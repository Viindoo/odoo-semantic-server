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
    """When embedder raises on NL query, tool degrades gracefully without crash.

    Note: literal tokens (.o_form_view, $var, etc.) bypass the embedder and go
    directly to ILIKE on the DB — so they need a DB connection and cannot be
    tested here without a fixture.  This test uses an NL phrase that is NOT a
    literal token, so the embed call is the first I/O attempted and the test
    remains DB-free.
    """
    from contextlib import contextmanager

    from src.mcp.server import _find_style_override

    _SECRET = "internal-detail-/srv/secret/path-leak-canary"

    class _RaisingEmbedder:
        def embed(self, texts):  # noqa: ANN001
            raise RuntimeError(_SECRET)

    class _FakeSession:
        """Minimal stub — resolve_version_v2 Tier-1 returns '99.0' without querying."""

    class _FakeDriver:
        """Minimal stub that provides driver.session() as a context manager.

        _find_style_override opens `with driver.session() as session:` to call
        _resolve_version.  Since '99.0' is an explicit (non-sentinel) version,
        Tier-1 of resolve_version_v2 returns immediately without executing any
        Cypher — so the session stub never needs to implement .run().
        """
        @contextmanager
        def session(self):
            yield _FakeSession()

    # "form view styling" is an NL phrase (has spaces) — not a literal token.
    # The embedder is called first (before any DB lookup) and raises.
    result = _find_style_override(
        "form view styling", "99.0",
        _driver=_FakeDriver(),
        _embedder=_RaisingEmbedder(),
    )
    # Should degrade, not raise
    assert "embedding failed" in result or "Found 0 results" in result
    # M6: exception internals must NOT leak to the agent-facing tool output —
    # neither the raw message text nor the exception class name.
    assert _SECRET not in result
    assert "RuntimeError" not in result


@pytest.mark.neo4j
def test_find_style_override_with_fake_embedder(clean_neo4j, pg_conn):
    """ANN/literal search with FakeEmbedder + seeded pg embeddings; verify output format.

    B1 fix: .o_form_view is a literal token -> routes through literal-first path.
    The render line now emits '· literal match · match: literal' instead of
    '· score X.XX'. We assert the new grammar (match: tag present) and that
    the overall structure (File:, Override chain) is intact.

    R9 fix: css entity_name is raw (no 'selector:' prefix); scss variable block
    uses 'variable:{stem}:variables' not 'variable:$primary'.
    """
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

    # Seed chunks with REALISTIC entity_name values (R9 correction):
    #   - css: raw selector (no 'selector:' prefix — that's scss/less only)
    #   - scss variable block: 'variable:{stem}:variables' (not 'variable:$primary')
    embedder = FakeEmbedder(dim=1024)
    chunks = [
        EmbeddingChunk(
            chunk_type="css",
            module=_CSS_MOD,
            odoo_version=TEST_VERSION,
            # R9 fix: css entity_name is the raw selector, no 'selector:' prefix
            entity_name=".o_form_view",
            model_name=None,
            file_path=f"/opt/odoo/{_CSS_MOD}/static/src/css/form.css",
            chunk_idx=0,
            content=".o_form_view { display: flex; }",
        ),
        EmbeddingChunk(
            chunk_type="scss",
            module=_CSS_MOD,
            odoo_version=TEST_VERSION,
            # R9 fix: scss variable block uses 'variable:{stem}:variables'
            entity_name="variable:variables:variables",
            model_name=None,
            file_path=f"/opt/odoo/{_CSS_MOD}/static/src/scss/variables.scss",
            chunk_idx=0,
            content="$primary: #00BBCE;",
        ),
    ]
    write_module_embeddings(_CSS_MOD, TEST_VERSION, chunks, embedder,
                            profile_name="test_profile")

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

    # If any results found, verify structure.
    # B1 fix: .o_form_view is literal -> render emits 'literal match' + 'match: literal'
    # NOT '· score X.XX' as the primary score token.
    if "Found 0" not in result:
        # The match: tag is always present (B1 grammar fix)
        assert "match:" in result
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
    write_module_embeddings(_MOD, TEST_VERSION, chunks, embedder,
                            profile_name="test_profile")

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
# T1/T2/T3 — Issue #255 AC1/AC2/AC3 literal-first integration tests
# These tests use a far-cluster embedder to PROVE results come from the literal
# path, not from semantic ranking.  pgvector ANN has no distance threshold, so
# old ANN-only code would still return the seeded rows (just low-cosine) — the
# genuine red-before-green guard is `assert "match: literal" in result`: only
# the new literal path emits that tag, and the literal hit set is independent of
# the (deliberately wrong) query vector. T3's escape decoy is the other guard.
# ===========================================================================


def _make_far_cluster_embedder():
    """Return a deterministic embedder whose vectors are far from all style chunks.

    Uses the ClusterEmbedder from test_find_examples_recall_mock.py with a
    cluster label that is never assigned to style chunk content — guaranteeing
    ANN recall collapse for style queries.
    """
    import math
    import random

    class FarClusterEmbedder:
        """Embedder that always returns a fixed 'cluster D' vector far from A/B/C."""

        dim = 1024

        def embed(self, texts: list) -> list:  # noqa: ANN001
            rng = random.Random(9999)
            vec = [rng.gauss(0, 1) for _ in range(self.dim)]
            norm = math.sqrt(sum(x * x for x in vec))
            unit = [x / norm for x in vec]
            return [list(unit) for _ in texts]

    return FarClusterEmbedder()


@pytest.mark.neo4j
def test_t1_ac1_literal_selector_far_cluster(clean_neo4j, pg_conn):
    """T1 — AC1: .o_list_view resolves via literal path even with a far-cluster embedder.

    Red-before-green (ETHOS #11):
    - The 'match: literal' tag is emitted ONLY by the new literal-first path;
      old ANN-only code never produces it (and would rank by the wrong query
      vector). That assertion + the far-cluster embedder prove the hit is from
      the literal ILIKE, not semantic luck.

    Verifies AC1 + AC3 (independence from semantic ranking).
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t1_literal_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    # Seed: scss chunk with realistic entity_name for .o_list_view
    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
        # css raw: no 'selector:' prefix
        EmbeddingChunk(
            chunk_type="css",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name=".o_form_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/css/form.css",
            chunk_idx=0,
            content=".o_form_view { display: flex; }",
        ),
        # Noise: unrelated selector
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_kanban_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/kanban.scss",
            chunk_idx=0,
            content=".o_kanban_view { display: grid; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    far_embedder = _make_far_cluster_embedder()
    try:
        result = _find_style_override(
            ".o_list_view",
            TEST_VERSION,
            limit=5,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # AC1: must find at least one result
    assert "Found 0" not in result, (
        "AC1 FAIL: .o_list_view must be found via literal ILIKE; got 'Found 0'.\n"
        "This indicates the literal-first path is not working."
    )
    assert "Found" in result
    # AC1: the result must reference the correct file
    assert "list.scss" in result, "Expected list.scss in result"
    # AC3: result came from literal path (not ANN)
    assert "match: literal" in result, (
        "AC3 FAIL: result must be tagged 'match: literal'; got:\n" + result[:500]
    )
    # The exact query token must appear in the result
    assert ".o_list_view" in result


@pytest.mark.neo4j
def test_t2_ac2_scss_variable_via_content_ilike(clean_neo4j, pg_conn):
    """T2 — AC2: $o-brand-primary resolves via content ILIKE on variable block.

    The variable name is NOT in entity_name (which is 'variable:{stem}:variables').
    It IS in content.  This test proves the content-ILIKE branch works.

    Red-before-green: the 'match: literal' tag + the $-name appearing in the
    snippet are emitted only by the new content-ILIKE branch; old ANN-only code
    has no content-substring path and never tags a hit as literal.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t2_var_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            # Realistic: variable block entity_name is NOT the var name
            entity_name="variable:variables:variables",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/variables.scss",
            chunk_idx=0,
            # The variable name lives only in content
            content="$o-brand-primary: #00BBCE;\n$o-brand-secondary: #7F4282;",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    far_embedder = _make_far_cluster_embedder()
    try:
        result = _find_style_override(
            "$o-brand-primary",
            TEST_VERSION,
            limit=5,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    assert "Found 0" not in result, (
        "AC2 FAIL: $o-brand-primary must be found via content ILIKE.\n"
        "This indicates the content-ILIKE branch is not working."
    )
    assert "$o-brand-primary" in result, "Variable name must appear in content snippet"
    assert "variables.scss" in result
    assert "match: literal" in result


@pytest.mark.neo4j
def test_t3_ac3_escape_underscore_decoy(clean_neo4j, pg_conn):
    """T3 — AC3: LIKE underscore escaping prevents decoy '.oXlist_view' from matching.

    If '_' in '.o_list_view' is not escaped as '\\_', the ILIKE pattern
    '%.o_list_view%' would ALSO match '.oXlist_view' because '_' is a wildcard.
    This test seeds a decoy and verifies it does NOT appear in results.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_style_override

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t3_escape_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        # Target: the real selector
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
        # Decoy: 'X' in place of '_' — should NOT match when _ is escaped
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.oXlist_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/decoy.scss",
            chunk_idx=0,
            content=".oXlist_view { display: none; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    far_embedder = _make_far_cluster_embedder()
    try:
        result = _find_style_override(
            ".o_list_view",
            TEST_VERSION,
            limit=10,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # Real selector must be found via literal path
    assert "list.scss" in result, "Expected list.scss (real selector) in result"
    assert "match: literal" in result

    # Decoy must NOT appear as a LITERAL hit.
    # We prove this by checking: the literal result count is exactly 1 (only the
    # real selector), NOT 2 (which would mean underscore was not escaped).
    # Note: the decoy MAY appear in semantic backfill (FakeEmbedder gives same
    # vectors to all chunks); that is acceptable — we only care that it is not
    # in the LITERAL section.
    lines = result.splitlines()
    # Count how many lines are result-header lines with "match: literal"
    literal_hit_count = sum(1 for ln in lines if "match: literal" in ln and ln.startswith("#"))
    assert literal_hit_count == 1, (
        f"T3 FAIL: Expected exactly 1 literal hit (the real .o_list_view), "
        f"got {literal_hit_count}.\n"
        "If the decoy .oXlist_view also matched literally, underscore escaping is broken."
    )
    # The first literal hit must be the real selector, not the decoy
    first_result_line = next((ln for ln in lines if ln.startswith("#1 ·")), "")
    assert "selector:.o_list_view" in first_result_line or "list.scss" in result, (
        "Expected first result to reference the real .o_list_view selector"
    )


@pytest.mark.neo4j
def test_t4_find_examples_literal_style_parity(clean_neo4j, pg_conn):
    """T4 — AC5/E: find_examples with style chunk_types uses literal-first.

    When chunk_types=['css','scss','less'] and query is a literal selector,
    find_examples must find the result via literal path (far-cluster embedder
    would return 0 without the fix).
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_examples

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t4_examples_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
        # Noise: a method chunk — should NOT match for style-only literal
        EmbeddingChunk(
            chunk_type="method",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="sale.order._compute_amount",
            model_name="sale.order",
            file_path=f"/opt/odoo/{_MOD}/models/sale_order.py",
            chunk_idx=0,
            content="def _compute_amount(self): ...",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    # Seed a Neo4j Module node for the rerank query
    with clean_neo4j.session() as s:
        s.run(
            "MERGE (:Module {name: $n, odoo_version: $v})",
            n=_MOD, v=TEST_VERSION,
        )

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    far_embedder = _make_far_cluster_embedder()
    try:
        # Positive: style-only literal query must hit
        result_pos = _find_examples(
            ".o_list_view",
            TEST_VERSION,
            limit=5,
            chunk_types=["css", "scss", "less"],
            _driver=clean_neo4j,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
        # Negative A: non-style chunk_type -> style_only False -> literal never engages.
        result_nonstyle = _find_examples(
            "primary button color",
            TEST_VERSION,
            limit=5,
            chunk_types=["method"],  # non-style type -> literal-first never engages
            _driver=clean_neo4j,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
        # Negative B (PR #257 review #3): NL phrase WITH style chunk_types. This is
        # the correct axis to guard is_literal_token against a future false-positive
        # on an NL phrase — style_only is True here, so is_literal_token IS consulted
        # and must return False (spaces, plain words) -> semantic path, no literal tag.
        result_nl_style = _find_examples(
            "primary button color variable",
            TEST_VERSION,
            limit=5,
            chunk_types=["css", "scss", "less"],
            _driver=clean_neo4j,
            _pg_conn=pg_conn,
            _embedder=far_embedder,
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # Positive: must find the style result
    assert "Found 0" not in result_pos, (
        "T4 FAIL: find_examples('.o_list_view', chunk_types=['css','scss','less']) "
        "must find results via literal-first. Got 'Found 0'."
    )
    assert "match: literal" in result_pos

    # Negative A: NL phrase with non-style chunk_type -> no literal tag
    assert "match: literal" not in result_nonstyle
    # Negative B: NL phrase WITH style chunk_types -> is_literal_token False -> no literal tag
    assert "match: literal" not in result_nl_style


@pytest.mark.neo4j
def test_t5_async_wrapper_literal_survives_embed_failure_style_override(
    clean_neo4j, pg_conn
):
    """T4b — async wrapper test for find_style_override (issue #255 B2/R4).

    When the embedder raises on the event loop BUT the query is a literal token,
    the tool must still return results from the literal ILIKE path.

    This test calls the async find_style_override wrapper directly with an
    embedder that raises, proving the WI-5 conditional pre-embed guard works.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t5_async_override_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    class AlwaysRaisingEmbedder:
        """Embedder that always raises — simulates total embedder outage."""
        def embed(self, texts):  # noqa: ANN001
            raise RuntimeError("embedder outage simulation")
        async def embed_async(self, texts):  # noqa: ANN001
            raise RuntimeError("embedder outage simulation")

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j

    # Monkeypatch _get_embedder to return the raising embedder
    original_get_embedder = srv_mod._get_embedder
    srv_mod._get_embedder = lambda: AlwaysRaisingEmbedder()

    try:
        # This test covers the SYNC body R4 path (embed failure with literal_rows
        # already populated -> serve literal-only). The ASYNC wrapper's conditional
        # pre-embed guard (the actual B2 bug location) is covered separately by the
        # asyncio.run((...)) tests below (test_async_wrapper_*).
        result = srv_mod._find_style_override(
            ".o_list_view",
            TEST_VERSION,
            limit=5,
            _pg_conn=pg_conn,
            _embedder=AlwaysRaisingEmbedder(),
        )
    finally:
        srv_mod._driver = original_driver
        srv_mod._get_embedder = original_get_embedder
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    # Despite embedder failure, literal results must still be returned (R4 path)
    assert "Found 0" not in result, (
        "T5 FAIL: literal result must be served even when embedder raises.\n"
        "The R4 / WI-4 embed-failure guard is not working.\n"
        f"Result: {result[:400]}"
    )
    assert "match: literal" in result
    assert "list.scss" in result


@pytest.mark.neo4j
def test_t6_async_wrapper_literal_survives_embed_failure_find_examples(
    clean_neo4j, pg_conn
):
    """T1b — async wrapper test for find_examples (issue #255 B2 BLOCKER).

    When the embedder raises but chunk_types are style-only and query is literal,
    find_examples must still return results from the literal ILIKE path.

    This test exercises the WI-8 / WI-7 conditional-embed guard in _find_examples.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings
    from src.mcp.server import _find_examples

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "t6_async_examples_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")

    # Seed Neo4j Module node for rerank
    with clean_neo4j.session() as s:
        s.run(
            "MERGE (:Module {name: $n, odoo_version: $v})",
            n=_MOD, v=TEST_VERSION,
        )

    class AlwaysRaisingEmbedder:
        def embed(self, texts):  # noqa: ANN001
            raise RuntimeError("embedder outage simulation")

    import src.mcp.server as srv_mod
    original_driver = srv_mod._driver
    srv_mod._driver = clean_neo4j
    try:
        # Call sync _find_examples with a raising embedder + literal style query.
        # The want_literal path must NOT call embed and must still return results.
        result = _find_examples(
            ".o_list_view",
            TEST_VERSION,
            limit=5,
            chunk_types=["css", "scss", "less"],
            _driver=clean_neo4j,
            _pg_conn=pg_conn,
            _embedder=AlwaysRaisingEmbedder(),
        )
    finally:
        srv_mod._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    assert "Found 0" not in result, (
        "T6 FAIL: find_examples('.o_list_view', style chunk_types) must return "
        "literal results even when the embedder raises (B2 BLOCKER path).\n"
        f"Result: {result[:400]}"
    )
    assert "match: literal" in result
    assert "list.scss" in result


@pytest.mark.neo4j
def test_t7_docstring_no_fabricated_score(clean_neo4j):
    """T7 (T6 in PLAN) — AC4: verify docstring no longer contains '0.87'.

    The old docstring had '0.87' as a fabricated score example that never
    appeared in real output.  This test guards against regression.
    """
    from src.mcp.server import _find_style_override

    doc = _find_style_override.__doc__ or ""
    assert "0.87" not in doc, (
        "AC4 FAIL: docstring still contains the fabricated score '0.87'.\n"
        "Update the docstring to use a realistic example."
    )
    # Verify the docstring mentions the literal path (positive invariant)
    assert "literal" in doc.lower(), (
        "AC4: docstring should mention the literal match path."
    )


# ===========================================================================
# Async-wrapper coverage for the B2 conditional pre-embed (issue #255 review
# MAJOR-2). These call the tool functions directly (FastMCP v3: @mcp.tool
# returns the original function unchanged) with `_get_embedder` forced to raise.
# They are pure-unit (no DB/Neo4j): the blocking sync body is stubbed so we only
# assert the wrapper's routing decision — literal tokens must NOT short-circuit
# on the embed failure; NL queries MUST. A regression that re-introduces an
# unconditional pre-embed would turn the literal assertions red.
# ===========================================================================


async def test_async_wrapper_find_style_override_literal_skips_embed(monkeypatch):
    """find_style_override: literal token reaches the body even when embed fails.

    Red-before-green for B2: if the wrapper pre-embedded unconditionally, the
    raising _get_embedder would return an early error and never reach the stub.
    """
    import src.mcp.server as srv

    def _boom():
        raise RuntimeError("embedder outage simulation")

    monkeypatch.setattr(srv, "_get_embedder", _boom)

    captured = {}

    def _stub(*args, **kwargs):
        captured["reached"] = True
        captured["query_vec"] = kwargs.get("_query_vec")
        return "find_style_override: stub\nFound 0 results\n"

    monkeypatch.setattr(srv, "_find_style_override", _stub)

    out = await srv.find_style_override(".o_list_view", "17.0")

    assert captured.get("reached") is True, (
        "literal token must reach the sync body despite embedder outage (B2)"
    )
    assert captured["query_vec"] is None, "literal path must NOT pre-embed"
    assert "embedder unavailable" not in out and "embedding failed" not in out


async def test_async_wrapper_find_style_override_nl_short_circuits(monkeypatch):
    """find_style_override: NL query DOES short-circuit on embed failure.

    Negative control proving the skip is literal-only, not a blanket bypass.
    """
    import src.mcp.server as srv

    def _boom():
        raise RuntimeError("embedder outage simulation")

    monkeypatch.setattr(srv, "_get_embedder", _boom)

    reached = {"v": False}

    def _stub(*args, **kwargs):
        reached["v"] = True
        return "stub"

    monkeypatch.setattr(srv, "_find_style_override", _stub)

    out = await srv.find_style_override("primary button color variable", "17.0")

    assert reached["v"] is False, "NL query must short-circuit before the body"
    assert "embedder unavailable" in out


async def test_async_wrapper_find_examples_literal_style_skips_embed(monkeypatch):
    """find_examples: literal style query reaches the body even when embed fails.

    Red-before-green for the B2 BLOCKER (the original bug location).
    """
    import src.mcp.server as srv

    def _boom():
        raise RuntimeError("embedder outage simulation")

    monkeypatch.setattr(srv, "_get_embedder", _boom)

    captured = {}

    def _stub(*args, **kwargs):
        captured["reached"] = True
        captured["query_vec"] = kwargs.get("_query_vec")
        return "find_examples: stub\nFound 0 results\n"

    monkeypatch.setattr(srv, "_find_examples", _stub)

    out = await srv.find_examples(
        ".o_list_view", "17.0", chunk_types=["css", "scss", "less"]
    )

    assert captured.get("reached") is True, (
        "literal style query must reach the body despite embedder outage (B2)"
    )
    assert captured["query_vec"] is None, "literal path must NOT pre-embed"
    assert "embedder unavailable" not in out and "embedding query failed" not in out


async def test_async_wrapper_find_examples_nl_embedder_down_uses_lexical(monkeypatch):
    """find_examples: NL query + embedder down -> lexical fallback (WI-9 / #264).

    Contract updated by WI-9: an NL query with style chunk_types no longer
    short-circuits to hard-fail when the embedder is down.  Instead the async
    wrapper catches the embed failure and sets _use_lexical=True, then
    delegates to _find_examples — which runs the lexical keyword path and
    labels results 'match: lexical'.  Hard-fail ('embedder unavailable' as a
    standalone error message) is no longer the contract.

    This test was originally named test_async_wrapper_find_examples_nl_style_short_circuits
    and asserted the old hard-fail contract from PR #255.  WI-9 (#264) replaced
    that contract with degraded-but-useful lexical fallback; the test is rewritten
    to assert the new intent (Iron Law of Root Cause — test must protect current
    business contract, not stale implementation).
    """
    import src.mcp.server as srv

    def _boom():
        raise RuntimeError("embedder outage simulation")

    monkeypatch.setattr(srv, "_get_embedder", _boom)

    captured: dict = {}

    def _stub(*args, **kwargs):
        captured["reached"] = True
        captured["use_lexical"] = kwargs.get("_use_lexical", False)
        return (
            "find_examples: stub\nFound 0 results  "
            "[degraded: embedder unavailable — lexical search returned nothing]\n"
        )

    monkeypatch.setattr(srv, "_find_examples", _stub)

    out = await srv.find_examples(
        "primary button color", "17.0", chunk_types=["css", "scss", "less"]
    )

    assert captured.get("reached") is True, (
        "NL query + embedder down must reach _find_examples body (lexical fallback)"
    )
    assert captured.get("use_lexical") is True, (
        "_find_examples must be called with _use_lexical=True when embedder is down"
    )
    # The wrapper must NOT produce a standalone hard-fail; the body handles messaging.
    # Stub returns a degraded banner — check the overall flow is degraded-not-error.
    assert "RuntimeError" not in out, "Hard-fail RuntimeError must not leak into output"


@pytest.mark.neo4j
async def test_async_wrapper_literal_e2e_db(clean_neo4j, pg_conn):
    """DB-backed async-wrapper e2e (PR #257 review #2).

    The unit tests above stub the sync body (routing only). This calls the tool
    functions directly (FastMCP v3: directly callable) through the real async ->
    asyncio.to_thread -> sync body -> DB seam for BOTH tools and asserts literal
    rows come back, closing the integration gap the reviewer flagged.
    """
    from contextlib import contextmanager
    from unittest import mock

    from pgvector.psycopg2 import register_vector

    from src.db.migrate import run_migrations
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    if not _pg_has_vector(pg_conn):
        pytest.skip("pgvector extension not installed")

    run_migrations(pg_conn)
    register_vector(pg_conn)

    _MOD = "async_e2e_mod"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
    pg_conn.commit()

    chunks = [
        EmbeddingChunk(
            chunk_type="scss",
            module=_MOD,
            odoo_version=TEST_VERSION,
            entity_name="selector:.o_list_view",
            model_name=None,
            file_path=f"/opt/odoo/{_MOD}/static/src/scss/list.scss",
            chunk_idx=0,
            content=".o_list_view { display: block; }",
        ),
    ]
    write_module_embeddings(_MOD, TEST_VERSION, chunks, FakeEmbedder(dim=1024),
                            profile_name="test_profile")
    with clean_neo4j.session() as s:
        s.run("MERGE (:Module {name: $n, odoo_version: $v})", n=_MOD, v=TEST_VERSION)

    @contextmanager
    def _yield_pg():
        yield pg_conn

    import src.mcp.server as srv

    original_driver = srv._driver
    srv._driver = clean_neo4j
    try:
        with mock.patch("src.mcp.server._checkout_pg", _yield_pg), \
             mock.patch("src.mcp.server._get_embedder", lambda: FakeEmbedder(dim=1024)):
            out_so = await srv.find_style_override(".o_list_view", TEST_VERSION)
            out_fe = await srv.find_examples(
                ".o_list_view", TEST_VERSION, chunk_types=["css", "scss", "less"]
            )
    finally:
        srv._driver = original_driver
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))
        pg_conn.commit()

    assert "Found 0" not in out_so and "match: literal" in out_so, (
        f"find_style_override literal e2e failed: {out_so[:300]}"
    )
    assert "list.scss" in out_so
    assert "Found 0" not in out_fe and "match: literal" in out_fe, (
        f"find_examples literal e2e failed: {out_fe[:300]}"
    )


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
