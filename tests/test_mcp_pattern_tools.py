# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for M4.6 MCP tools.

Covers suggest_pattern / check_module_exists / find_override_point.
Requires Neo4j + PostgreSQL + pgvector (uses FakeEmbedder for deterministic vec).
"""
import pytest

from tests.conftest import PG_EMBED_VERSION as TEST_VERSION

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def seeded_patterns(clean_pg_embeddings, clean_neo4j):
    """Seed Neo4j PatternExample + PostgreSQL pattern_example chunks."""
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.models import PatternExample
    from src.indexer.writer_neo4j import Neo4jWriter
    from src.indexer.writer_pgvector import (
        _INSERT_SQL,
        make_pattern_chunks,
    )

    patterns = [
        PatternExample(
            pattern_id="t-computed-field-cross-model",
            intent_keywords=["compute", "depends", "cross-model"],
            file_ref="addons/sale/models/sale_order.py:1",
            snippet_text="@api.depends('partner_id.country_id')\ndef _compute(self): ...",
            gotchas=["Many2one root in path"],
            odoo_version_min=TEST_VERSION,
            language="python",
            core_symbol_names=[],
        ),
        PatternExample(
            pattern_id="t-xpath-avoid-replace",
            intent_keywords=["xpath", "replace"],
            file_ref="addons/sale/views/v.xml:1",
            snippet_text="<xpath position=\"after\">...</xpath>",
            gotchas=["position='replace' breaks downstream"],
            odoo_version_min=TEST_VERSION,
            language="xml",
            core_symbol_names=[],
        ),
    ]

    import os
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    writer.write_pattern_examples(patterns)
    writer.close()

    embedder = FakeEmbedder(dim=1024)
    chunks = make_pattern_chunks(patterns)
    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)
    from src.constants import GLOBAL_PROFILE
    for _c in chunks:
        _c.profile_name = GLOBAL_PROFILE
    from psycopg2.extras import execute_values
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )
    return clean_pg_embeddings, clean_neo4j


@pytest.fixture
def seeded_version_window_patterns(clean_pg_embeddings, clean_neo4j):
    """Seed test-* PatternExample nodes with VARIED [vmin, vmax] windows.

    #329 oracle fixture. The pgvector ANN query in _suggest_pattern does NOT
    filter by version (it ranks ALL '__patterns__' chunks); version selection is
    enforced purely by the post-fetch [vmin, vmax] window filter reading the
    Neo4j PatternExample nodes. So the embeddings are all stamped TEST_VERSION
    (99.0) - that keeps clean_pg_embeddings cleanup working (it deletes only
    odoo_version=99.0) WITHOUT weakening the test: the version semantics live
    entirely in the Neo4j window, which is exactly what WI-1 filters on.

    Windows mirror the real catalogue's era1/era2 split:
      - test-savepointcase-v8-v15   : 8.0 - 15.0  (excluded for a v17 query)
      - test-httpcase-tour-qunit-v17: 16.0 - 17.0 (INCLUDED for v17)
      - test-httpcase-tour-hoot-v18 : 18.0 - open (excluded for v17)
      - test-transaction-savepoint-v16plus / test-computed-field : 16.0 - open

    Teardown: DETACH DELETEs the PatternExample nodes written here by their
    pattern_ids. clean_neo4j only deletes nodes WHERE odoo_version = '99.0',
    and PatternExample uses odoo_version_min/max (not odoo_version), so without
    explicit teardown these nodes persist across the session and corrupt later
    tests that MERGE the same pattern_ids (test-order dependent).
    """
    import os

    from src.constants import GLOBAL_PROFILE
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.models import PatternExample
    from src.indexer.writer_neo4j import Neo4jWriter
    from src.indexer.writer_pgvector import _INSERT_SQL, make_pattern_chunks

    patterns = [
        PatternExample(
            pattern_id="test-savepointcase-v8-v15",
            intent_keywords=["transaction", "savepoint", "test"],
            file_ref="addons/base/tests/common.py:1",
            snippet_text="class Foo(SavepointCase): ...",
            gotchas=["SavepointCase merged into TransactionCase in v16"],
            odoo_version_min="8.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max="15.0",
            category="test",
        ),
        PatternExample(
            pattern_id="test-transaction-savepoint-v16plus",
            intent_keywords=["transaction", "savepoint", "test", "rollback"],
            file_ref="addons/base/tests/common.py:2",
            snippet_text="class Foo(TransactionCase): ...",
            gotchas=["use cr.savepoint() not cr.commit() in an isolation context"],
            odoo_version_min="16.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max=None,
            category="test",
        ),
        PatternExample(
            pattern_id="test-computed-field",
            intent_keywords=["compute", "test", "assertEqual"],
            file_ref="addons/sale/tests/test_compute.py:1",
            snippet_text="def test_amount(self): self.assertEqual(...)",
            gotchas=["recompute after write"],
            odoo_version_min="16.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max=None,
            category="test",
        ),
        PatternExample(
            pattern_id="test-httpcase-tour-qunit-v17",
            intent_keywords=["httpcase", "tour", "qunit", "test"],
            file_ref="addons/web/tests/test_tour.py:1",
            snippet_text="class Foo(HttpCase): self.start_tour(...)",
            gotchas=["QUnit suite replaced by Hoot in v18"],
            odoo_version_min="16.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max="17.0",
            category="test",
        ),
        PatternExample(
            pattern_id="test-httpcase-tour-hoot-v18",
            intent_keywords=["httpcase", "tour", "hoot", "test"],
            file_ref="addons/web/static/tests/foo.test.js:1",
            snippet_text="import { test } from '@odoo/hoot';",
            gotchas=["Hoot is v18+ only"],
            odoo_version_min="18.0",
            language="js",
            core_symbol_names=[],
            odoo_version_max=None,
            category="test",
        ),
    ]

    neo4j_uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_TEST_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    writer = Neo4jWriter(
        uri=neo4j_uri, user=neo4j_user, password=neo4j_password,
    )
    writer.setup_indexes()
    writer.write_pattern_examples(patterns)
    writer.close()

    embedder = FakeEmbedder(dim=1024)
    chunks = make_pattern_chunks(patterns)
    # Re-stamp every chunk to TEST_VERSION so clean_pg_embeddings reclaims them.
    # The ANN query ignores embedding.odoo_version, so this is loss-free for the
    # version semantics under test (those come from the Neo4j window).
    for _c in chunks:
        _c.odoo_version = TEST_VERSION
        _c.profile_name = GLOBAL_PROFILE
    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)
    from psycopg2.extras import execute_values
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )

    yield clean_pg_embeddings, clean_neo4j

    # Teardown: DETACH DELETE the PatternExample nodes written above.
    # clean_neo4j only sweeps nodes WHERE odoo_version = '99.0'; PatternExample
    # uses odoo_version_min/max so those nodes persist without this teardown,
    # corrupting tests that MERGE the same pattern_ids later in the session.
    seeded_pids = [p.pattern_id for p in patterns]
    with clean_neo4j.session() as session:
        session.run(
            "UNWIND $ids AS pid "
            "MATCH (p:PatternExample {pattern_id: pid}) "
            "DETACH DELETE p",
            ids=seeded_pids,
        )


@pytest.fixture
def seeded_category_oracle_patterns(clean_pg_embeddings, clean_neo4j):
    """Seed PatternExample nodes with non-prefix-aligned category values.

    WI-7 oracle fixture: proves filter reads p.category property, NOT pattern_id
    prefix. Two patterns break the old prefix assumption:
      - 'savepoint-helper' (category='test')  : id does NOT start with 'test-'
        -> must be returned when category='test'
      - 'test-production-tip' (category='production'): id STARTS with 'test-'
        -> must NOT be returned when category='test'

    Teardown: DETACH DELETE by pattern_id (same as seeded_version_window_patterns).
    """
    import os

    from src.constants import GLOBAL_PROFILE
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.models import PatternExample
    from src.indexer.writer_neo4j import Neo4jWriter
    from src.indexer.writer_pgvector import _INSERT_SQL, make_pattern_chunks

    patterns = [
        # category='test' but id does NOT start with 'test-'
        PatternExample(
            pattern_id="savepoint-helper",
            intent_keywords=["savepoint", "test", "isolation"],
            file_ref="addons/base/tests/common.py:10",
            snippet_text="with self.cr.savepoint(): ...",
            gotchas=["do not commit inside savepoint"],
            odoo_version_min="16.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max=None,
            category="test",
        ),
        # id starts with 'test-' but category='production'
        PatternExample(
            pattern_id="test-production-tip",
            intent_keywords=["production", "tip"],
            file_ref="addons/sale/models/sale_order.py:5",
            snippet_text="# production pattern",
            gotchas=[],
            odoo_version_min="16.0",
            language="python",
            core_symbol_names=[],
            odoo_version_max=None,
            category="production",
        ),
    ]

    neo4j_uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_TEST_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_TEST_PASSWORD", "password")

    writer = Neo4jWriter(uri=neo4j_uri, user=neo4j_user, password=neo4j_password)
    writer.setup_indexes()
    writer.write_pattern_examples(patterns)
    writer.close()

    embedder = FakeEmbedder(dim=1024)
    chunks = make_pattern_chunks(patterns)
    for _c in chunks:
        _c.odoo_version = TEST_VERSION
        _c.profile_name = GLOBAL_PROFILE
    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)
    from psycopg2.extras import execute_values
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )

    yield clean_pg_embeddings, clean_neo4j

    # Teardown: DETACH DELETE the PatternExample nodes written above.
    seeded_pids = [p.pattern_id for p in patterns]
    with clean_neo4j.session() as session:
        session.run(
            "UNWIND $ids AS pid "
            "MATCH (p:PatternExample {pattern_id: pid}) "
            "DETACH DELETE p",
            ids=seeded_pids,
        )


@pytest.fixture
def seeded_modules(clean_neo4j):
    """Seed Module nodes for check_module_exists tests."""
    import os

    from src.indexer.models import ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    sale = ModuleInfo(
        name="sale", odoo_version=TEST_VERSION, repo="odoo",
        path="/odoo/addons/sale", depends=[], version_raw="",
        edition="community",
    )
    viin_helpdesk = ModuleInfo(
        name="viin_helpdesk", odoo_version=TEST_VERSION, repo="acme_addons17",
        path="/acme_addons17/viin_helpdesk", depends=[], version_raw="",
        edition="viindoo",
    )
    writer.write_results([
        ParseResult(module=sale, models=[]),
        ParseResult(module=viin_helpdesk, models=[]),
    ])
    writer.close()
    return clean_neo4j


@pytest.fixture
def seeded_method_chain(clean_neo4j):
    """Seed Method nodes (override chain) for find_override_point tests."""
    import os

    from src.indexer.models import (
        MethodInfo,
        ModelInfo,
        ModuleInfo,
        ParseResult,
    )
    from src.indexer.writer_neo4j import Neo4jWriter
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Base + 2 extension modules — action_confirm with super() in extensions
    modules = [
        ("sale", "community", False),   # base: no super()
        ("viin_sale", "viindoo", True),
        ("to_sale_custom", "viindoo", True),
    ]
    results = []
    for mod_name, edition, has_super in modules:
        module = ModuleInfo(
            name=mod_name, odoo_version=TEST_VERSION, repo="r",
            path=f"/p/{mod_name}", depends=[], version_raw="",
            edition=edition,
        )
        model = ModelInfo(
            name="sale.order", module=mod_name, odoo_version=TEST_VERSION,
            methods=[
                MethodInfo(
                    name="action_confirm", has_super_call=has_super,
                    convention_kind="action", super_safety="always",
                    return_required=True,
                ),
                MethodInfo(
                    name="_compute_amount", has_super_call=False,
                    convention_kind="compute", super_safety="never",
                    return_required=False,
                ),
            ],
        )
        results.append(ParseResult(module=module, models=[model]))
    writer.write_results(results)
    writer.close()
    return clean_neo4j


# --- suggest_pattern tests --------------------------------------------------


class TestSuggestPattern:
    def test_returns_header_and_match(self, seeded_patterns):
        pg, neo4j_driver = seeded_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "computed field cross-model partner",
            odoo_version=TEST_VERSION,
            language="python",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        assert result.startswith("suggest_pattern(")
        assert "matches" in result
        assert "t-computed-field-cross-model" in result

    def test_language_filter_python_only(self, seeded_patterns):
        pg, neo4j_driver = seeded_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "anything", odoo_version=TEST_VERSION, language="python",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        # XML pattern should NOT appear under language=python filter
        assert "t-xpath-avoid-replace" not in result

    def test_language_all_returns_xml_too(self, seeded_patterns):
        pg, neo4j_driver = seeded_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "anything", odoo_version=TEST_VERSION, language="all",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        # Both should be in scope when language='all'
        assert (
            "t-computed-field-cross-model" in result
            or "t-xpath-avoid-replace" in result
        )

    def test_empty_intent_rejected(self):
        from src.mcp.server import _suggest_pattern
        result = _suggest_pattern("", odoo_version=TEST_VERSION)
        assert "intent is required" in result

    def test_invalid_language_rejected(self):
        from src.mcp.server import _suggest_pattern
        result = _suggest_pattern("x", odoo_version=TEST_VERSION, language="fortran")
        assert "invalid language" in result

    def test_includes_gotchas_in_output(self, seeded_patterns):
        pg, neo4j_driver = seeded_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "computed field", odoo_version=TEST_VERSION, language="python",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        assert "Gotchas" in result
        assert "Many2one root" in result

    def test_suggest_pattern_category_test_returns_results_after_seed(
        self, seeded_version_window_patterns,
    ):
        """#329 regression: a v17 category='test' query surfaces in-range test
        patterns, NOT the empty 'No patterns found' branch.

        Guards against the version filter over-pruning: if the [vmin, vmax]
        window check wrongly rejected open-ended (max=None) patterns, every
        test pattern would vanish and the tool would falsely claim the
        catalogue is empty. The in-range v16+ test patterns MUST appear.
        """
        pg, neo4j_driver = seeded_version_window_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "write a transaction test with savepoint rollback",
            odoo_version="17.0",
            language="all",
            limit=20,
            category="test",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        # An open-ended (max=None) v16+ pattern is valid for v17 -> must render.
        assert "test-transaction-savepoint-v16plus" in result
        # The tool must NOT report the catalogue as empty for this query.
        assert "No patterns found" not in result
        assert "No curated patterns are valid" not in result

    def test_suggest_pattern_excludes_out_of_range_version(
        self, seeded_version_window_patterns,
    ):
        """#329 core correctness oracle: a v17 query MUST drop patterns whose
        [vmin, vmax] window excludes v17, and KEEP the one that covers it.

        - test-savepointcase-v8-v15 (max 15.0) -> EXCLUDED (15.0 < 17.0).
        - test-httpcase-tour-hoot-v18 (min 18.0) -> EXCLUDED (18.0 > 17.0).
        - test-httpcase-tour-qunit-v17 (16.0-17.0) -> INCLUDED (boundary).

        Numeric compare is load-bearing: lexicographically "8.0" > "15.0" and
        "18.0" < "8.0", so a string compare would mis-classify both excluded
        patterns. Asserting on rendered pattern IDs (observable output), not on
        internal call counts.
        """
        pg, neo4j_driver = seeded_version_window_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "httpcase tour test savepoint",
            odoo_version="17.0",
            language="all",
            limit=20,
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        # Upper-bound excluded: v8-v15 pattern must NOT surface for a v17 query.
        assert "test-savepointcase-v8-v15" not in result, (
            "a v8-v15 pattern leaked into a v17 result - the upper-bound "
            "(odoo_version_max) filter is not applied"
        )
        # Lower-bound excluded: v18-only pattern must NOT surface for v17.
        assert "test-httpcase-tour-hoot-v18" not in result, (
            "a v18+ pattern leaked into a v17 result - the lower-bound filter "
            "is not applied"
        )
        # Boundary INCLUDED: the 16.0-17.0 pattern covers v17 exactly.
        assert "test-httpcase-tour-qunit-v17" in result, (
            "the 16.0-17.0 pattern was wrongly dropped for a v17 query - the "
            "window filter rejects the inclusive upper boundary"
        )

    def test_category_filter_reads_property_not_prefix(
        self, seeded_category_oracle_patterns,
    ):
        """WI-7 oracle: category filter reads p.category property, NOT pattern_id prefix.

        'savepoint-helper' has category='test' but id does NOT start with 'test-'.
        The old prefix-based hack would exclude it; the property-based filter keeps it.
        Assert: category='test' returns 'savepoint-helper'.
        """
        pg, neo4j_driver = seeded_category_oracle_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "savepoint isolation test",
            odoo_version="17.0",
            language="python",
            limit=20,
            category="test",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        assert "savepoint-helper" in result, (
            "pattern with category='test' and non-'test-' prefix must be returned "
            "when category='test' - prefix-based filter would wrongly exclude it"
        )

    def test_category_filter_excludes_wrong_category_despite_prefix(
        self, seeded_category_oracle_patterns,
    ):
        """WI-7 oracle: pattern id starting with 'test-' but category='production'
        must NOT appear when filtering by category='test'.

        The old prefix-based hack would include 'test-production-tip' (its id
        starts with 'test-'). The property-based filter correctly excludes it.
        """
        pg, neo4j_driver = seeded_category_oracle_patterns
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _suggest_pattern

        result = _suggest_pattern(
            "production tip pattern",
            odoo_version="17.0",
            language="python",
            limit=20,
            category="test",
            _driver=neo4j_driver, _pg_conn=pg,
            _embedder=FakeEmbedder(dim=1024),
        )
        assert "test-production-tip" not in result, (
            "pattern with id starting 'test-' but category='production' must be "
            "excluded when category='test' - old prefix hack would wrongly include it"
        )


# --- _format_suggest_pattern unit tests (no DB) ----------------------------
#
# These test _format_suggest_pattern directly (no ANN / no Neo4j / no PG) to
# prove the over-fetch+truncate fix (Finding 1). pytestmark is postgres+neo4j
# at module level but these tests do NOT touch either service; they pass
# unconditionally because _format_suggest_pattern is pure Python.


class TestFormatSuggestPatternTruncation:
    """Unit tests for the version-filter + limit-truncation in
    _format_suggest_pattern. No DB required.

    Red-green contract (Finding 1):
      - BEFORE the fix (no `limit` param / no truncation line), calling
        _format_suggest_pattern with 3 out-of-version + 5 in-version ids and
        limit=5 either raises TypeError (unexpected kwarg) or returns all 8
        in-version ids untruncated (> limit).
      - AFTER the fix, the function accepts `limit`, applies it AFTER the
        version filter, and returns exactly the 5 in-version ids in score order
        with none of the 3 out-of-version ids.
    """

    def _make_rec(self, vmin, vmax):
        return {
            "lang": "python",
            "vmin": vmin,
            "vmax": vmax,
            "fr": "addons/sale/models/s.py:1",
            "sn": "def foo(): pass",
            "g": [],
            "category": "test",
        }

    def test_truncate_to_limit_after_version_filter(self):
        """5 in-version ids returned; 3 out-of-version ids excluded; total = limit."""
        from src.mcp.tools.guidance import _format_suggest_pattern

        # 3 out-of-version (v8-v15 only) + 5 in-version (v16+, open-ended)
        out_ids = [f"old-pattern-{i}" for i in range(3)]
        in_ids = [f"new-pattern-{i}" for i in range(5)]
        ordered_ids = out_ids + in_ids

        by_id = {}
        for pid in out_ids:
            by_id[pid] = self._make_rec("8.0", "15.0")
        for pid in in_ids:
            by_id[pid] = self._make_rec("16.0", None)

        score_map = {pid: 0.9 - i * 0.05 for i, pid in enumerate(ordered_ids)}

        result = _format_suggest_pattern(
            ordered_ids=ordered_ids,
            by_id=by_id,
            score_map=score_map,
            intent="test truncation",
            version="17.0",
            language="python",
            limit=5,
        )

        # All 5 in-version ids must appear.
        for pid in in_ids:
            assert pid in result, f"in-version pattern {pid!r} missing from output"

        # None of the 3 out-of-version ids must appear.
        for pid in out_ids:
            assert pid not in result, (
                f"out-of-version pattern {pid!r} leaked into v17 output"
            )

        # The match count header must say exactly 5 (truncated to limit).
        assert "5 matches" in result, (
            f"expected '5 matches' in header after truncation, got: {result[:200]!r}"
        )

        # F2: Category line must appear in output (red-green: fails if line is absent).
        assert "Category:" in result, (
            f"expected 'Category:' line in _format_suggest_pattern output (F2 fix), "
            f"got: {result[:400]!r}"
        )

    def test_empty_after_filter_does_not_truncate(self):
        """When ALL ids are out-of-version, the empty-after-filter branch fires
        before truncation (truncation of an empty list is a no-op, but the
        important thing is the right branch message is returned)."""
        from src.mcp.tools.guidance import _format_suggest_pattern

        out_ids = ["old-only-1", "old-only-2"]
        by_id = {pid: self._make_rec("8.0", "10.0") for pid in out_ids}
        score_map = {pid: 0.8 for pid in out_ids}

        result = _format_suggest_pattern(
            ordered_ids=out_ids,
            by_id=by_id,
            score_map=score_map,
            intent="no valid patterns",
            version="17.0",
            language="python",
            limit=5,
        )

        assert "No curated patterns are valid" in result
        for pid in out_ids:
            assert pid not in result


# --- check_module_exists tests ----------------------------------------------


class TestCheckModuleExists:
    def test_indexed_community_module(self, seeded_modules):
        from src.mcp.server import _check_module_exists
        result = _check_module_exists(
            "sale", odoo_version=TEST_VERSION, _driver=seeded_modules,
        )
        assert "Indexed:         Yes" in result
        assert "community" in result.lower()
        assert "Is EE confusion: No" in result

    def test_ee_confusion_not_indexed_with_warning(self, seeded_modules):
        from src.mcp.server import _check_module_exists
        result = _check_module_exists(
            "knowledge", odoo_version=TEST_VERSION, _driver=seeded_modules,
        )
        assert "Indexed:         No" in result
        assert "Is EE confusion: Yes" in result
        assert "WARNING" in result
        assert "Do NOT" in result

    def test_ee_confusion_with_viindoo_equivalent(self, seeded_modules):
        from src.mcp.server import _check_module_exists
        result = _check_module_exists(
            "helpdesk", odoo_version=TEST_VERSION, _driver=seeded_modules,
        )
        assert "Is EE confusion: Yes" in result
        assert "viin_helpdesk" in result

    def test_viindoo_module_indexed(self, seeded_modules):
        from src.mcp.server import _check_module_exists
        result = _check_module_exists(
            "viin_helpdesk", odoo_version=TEST_VERSION, _driver=seeded_modules,
        )
        assert "Indexed:         Yes" in result
        assert "viindoo" in result.lower()

    def test_unknown_module_not_indexed(self, seeded_modules):
        from src.mcp.server import _check_module_exists
        result = _check_module_exists(
            "nonexistent_xyz", odoo_version=TEST_VERSION, _driver=seeded_modules,
        )
        assert "Indexed:         No" in result
        assert "Is EE confusion: No" in result

    def test_check_module_exists_indexed_enterprise_not_in_dict(self, clean_neo4j):
        """Indexed genuine Odoo EE module (edition='enterprise', license OEEL-1)
        NOT in the legacy dict → EE warning sourced from the indexed graph,
        with the OEEL-1 license disclosed.

        A genuine Odoo S.A. Enterprise add-on ships under the OEEL-1 license
        (Odoo Enterprise Edition License); the indexer detects edition='enterprise'
        from it. WI-8 (#263) made the warning's license disclosure data-driven
        — it renders the module's actual indexed license rather than a hardcoded
        'OEEL-1' string — so a realistic genuine-EE module must carry license
        OEEL-1 to surface that disclosure.
        """
        import os

        from src.indexer.models import ModuleInfo, ParseResult
        from src.indexer.writer_neo4j import Neo4jWriter
        from src.mcp.server import _check_module_exists

        # Seed Module node: knowledge_pro with edition='enterprise' NOT in dict
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        knowledge_pro = ModuleInfo(
            name="knowledge_pro", odoo_version=TEST_VERSION, repo="odoo-ee",
            path="/odoo-ee/addons/knowledge_pro", depends=[], version_raw="",
            edition="enterprise", license="OEEL-1",
        )
        writer.write_results([ParseResult(module=knowledge_pro, models=[])])
        writer.close()

        result = _check_module_exists(
            "knowledge_pro", odoo_version=TEST_VERSION, _driver=clean_neo4j,
        )
        assert "Indexed:         Yes" in result
        assert "Is EE confusion: Yes" in result
        assert "license=OEEL-1" in result
        assert "WARNING" in result
        assert "Do NOT" in result

    def test_check_module_exists_not_indexed_in_dict_fallback(self, clean_neo4j):
        """Not indexed but in EE_CONFUSION dict → EE warning (dict fallback)."""
        from src.mcp.server import _check_module_exists

        # Pick "knowledge" from EE_CONFUSION dict (not indexed)
        result = _check_module_exists(
            "knowledge", odoo_version=TEST_VERSION, _driver=clean_neo4j,
        )
        assert "Indexed:         No" in result
        assert "Is EE confusion: Yes" in result
        assert "legacy hardcoded dict" in result
        assert "WARNING" in result
        assert "Do NOT" in result


# --- find_override_point tests ----------------------------------------------


class TestFindOverridePoint:
    def test_action_method_super_ratio_2_over_3(self, seeded_method_chain):
        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_confirm", odoo_version=TEST_VERSION,
            _driver=seeded_method_chain,
        )
        assert "Convention:      action" in result
        assert "Super safety:    always" in result
        assert "Return required: Yes" in result
        # 2 of 3 modules call super (viin_sale + to_sale_custom)
        assert "2/3" in result
        assert "Anti-patterns" in result

    def test_compute_method_super_never_anti_pattern(self, seeded_method_chain):
        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "_compute_amount", odoo_version=TEST_VERSION,
            _driver=seeded_method_chain,
        )
        assert "Convention:      compute" in result
        assert "Super safety:    never" in result
        # compute-specific anti-pattern
        assert "super()" in result.lower()

    def test_method_not_found(self, seeded_method_chain):
        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "no_such_method", odoo_version=TEST_VERSION,
            _driver=seeded_method_chain,
        )
        assert "method not found" in result.lower()


# --- profile_name filter tests for check_module_exists ---------------------


class TestCheckModuleExistsProfileFilter:
    """Verify profile_name backward compat and isolation for check_module_exists."""

    @pytest.fixture
    def seeded_module_profiles(self, clean_neo4j):
        """Seed Module nodes with distinct profiles."""
        import os

        from src.indexer.models import ModuleInfo, ParseResult
        from src.indexer.writer_neo4j import Neo4jWriter

        uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_TEST_USER", "neo4j")
        password = os.getenv("NEO4J_TEST_PASSWORD", "password")

        writer = Neo4jWriter(uri=uri, user=user, password=password)
        writer.setup_indexes()

        # sale module → profile "alpha_cme"
        mod_alpha = ModuleInfo("cme_alpha_mod", TEST_VERSION, "repo_alpha", "/tmp", [], "")
        writer.write_results(
            [ParseResult(module=mod_alpha, models=[])],
            profiles=["alpha_cme"],
        )

        # viin_sale module → profile "beta_cme"
        mod_beta = ModuleInfo("cme_beta_mod", TEST_VERSION, "repo_beta", "/tmp", [], "")
        writer.write_results(
            [ParseResult(module=mod_beta, models=[])],
            profiles=["beta_cme"],
        )

        writer.close()
        return uri, user, password

    def test_profile_none_backward_compat(self, seeded_module_profiles):
        """profile_name=None (default) finds modules from all profiles."""
        from neo4j import GraphDatabase

        from src.mcp.server import _check_module_exists

        uri, user, password = seeded_module_profiles
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            result = _check_module_exists(
                "cme_alpha_mod", odoo_version=TEST_VERSION, _driver=driver,
            )
        finally:
            driver.close()
        assert "Indexed:         Yes" in result

    def test_profile_filter_finds_correct_module(self, seeded_module_profiles):
        """profile_name='alpha_cme' finds the alpha module."""
        from neo4j import GraphDatabase

        from src.mcp.server import _check_module_exists

        uri, user, password = seeded_module_profiles
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            result = _check_module_exists(
                "cme_alpha_mod", odoo_version=TEST_VERSION,
                profile_name="alpha_cme", _driver=driver,
            )
        finally:
            driver.close()
        assert "Indexed:         Yes" in result

    def test_profile_name_narrows_non_escalating_for_admin(self, seeded_module_profiles):
        """WG-3t T3 (ADR-0034): profile_name is a NON-ESCALATING narrowing filter,
        consistent across the Neo4j and pgvector paths (fixes the split-brain).

        Pre-WG-3t the Neo4j path treated admin's profile_name as advisory (the beta
        module was found when asking under 'alpha_cme') while pgvector narrowed — a
        split-brain. Under T3 BOTH paths narrow: admin asking for 'alpha_cme' narrows
        to that profile, so cme_beta_mod (under a different profile) is NOT found, while
        the matching module still is. The tenant boundary remains the isolation
        guarantee (test_cross_tenant_isolation).
        """
        from neo4j import GraphDatabase

        from src.mcp.server import _check_module_exists

        uri, user, password = seeded_module_profiles
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            # beta module narrowed away when asking under the alpha profile.
            beta = _check_module_exists(
                "cme_beta_mod", odoo_version=TEST_VERSION,
                profile_name="alpha_cme", _driver=driver,
            )
            # matching profile still surfaces its own module (precise narrowing).
            alpha = _check_module_exists(
                "cme_alpha_mod", odoo_version=TEST_VERSION,
                profile_name="alpha_cme", _driver=driver,
            )
        finally:
            driver.close()
        assert "Indexed:         No" in beta, (
            f"profile_name='alpha_cme' must narrow away cme_beta_mod, got: {beta!r}"
        )
        assert "Indexed:         Yes" in alpha, (
            f"profile_name='alpha_cme' must still find cme_alpha_mod, got: {alpha!r}"
        )
