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
    from psycopg2.extras import execute_values
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )
    return clean_pg_embeddings, clean_neo4j


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
        """Indexed Module with edition='enterprise' (OEEL-1) → EE warning (indexed source)."""
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
            edition="enterprise",
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

    def test_profile_filter_hides_other_profile(self, seeded_module_profiles):
        """profile_name='alpha_cme' does not find the beta module."""
        from neo4j import GraphDatabase

        from src.mcp.server import _check_module_exists

        uri, user, password = seeded_module_profiles
        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            result = _check_module_exists(
                "cme_beta_mod", odoo_version=TEST_VERSION,
                profile_name="alpha_cme", _driver=driver,
            )
        finally:
            driver.close()
        assert "Indexed:         No" in result
