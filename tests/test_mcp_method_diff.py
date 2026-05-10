# tests/test_mcp_method_diff.py
"""Integration tests for find_override_point cross-version diff (M6 W3-7).

Uses Neo4j test fixtures with TEST_VERSION=99.0 / ALT_VERSION=98.0 to avoid
conflict with real indexed data. All tests require Neo4j (testcontainers or
local bolt).
"""
import os

import pytest

pytestmark = pytest.mark.neo4j

TEST_VERSION = "99.0"   # from_version in diff tests
ALT_VERSION = "98.0"    # to_version in diff tests


def _make_writer():
    from src.indexer.writer_neo4j import Neo4jWriter
    return Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


def _seed_method(
    driver,
    version: str,
    method_name: str,
    model_name: str = "sale.order",
    module_name: str = "sale",
    decorators: list[str] | None = None,
    signature: str | None = None,
    convention_kind: str = "action",
    super_safety: str = "always",
    has_super_call: bool = True,
) -> None:
    """Seed a single Method node (and parent Model + Module) for diff tests."""
    from src.indexer.models import MethodInfo, ModelInfo, ModuleInfo, ParseResult

    writer = _make_writer()
    writer.setup_indexes()
    module = ModuleInfo(
        name=module_name, odoo_version=version, repo="test",
        path=f"/test/{module_name}", depends=[], version_raw="",
    )
    method = MethodInfo(
        name=method_name,
        has_super_call=has_super_call,
        decorators=decorators or [],
        convention_kind=convention_kind,
        super_safety=super_safety,
        return_required=False,
        signature=signature,
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=version,
        methods=[method],
    )
    writer.write_results([ParseResult(module=module, models=[model])])
    writer.close()


def _clean_version(driver, version: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=version,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMethodVersionDiff:
    """Cross-version diff tests for find_override_point (M6 W3-7)."""

    def test_diff_decorator_removed(self, clean_neo4j):
        """Decorator present in from_version but absent in to_version shows as removed."""
        driver = clean_neo4j
        _clean_version(driver, ALT_VERSION)

        # from_version (99.0): has api.multi
        _seed_method(driver, TEST_VERSION, "action_confirm",
                     decorators=["api.multi"])
        # to_version (98.0): no decorators
        _seed_method(driver, ALT_VERSION, "action_confirm",
                     decorators=[])

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_confirm",
            odoo_version=TEST_VERSION,
            to_version=ALT_VERSION,
            _driver=driver,
        )

        assert "Method version diff" in result
        assert f"Removed in {ALT_VERSION}" in result
        assert "api.multi" in result
        # No "added" lines expected
        assert "Added in" not in result

        _clean_version(driver, ALT_VERSION)

    def test_diff_method_deleted_in_newer(self, clean_neo4j):
        """Method present only in from_version is reported as deleted in to_version."""
        driver = clean_neo4j
        _clean_version(driver, ALT_VERSION)

        # Only from_version seeded — to_version has no such method
        _seed_method(driver, TEST_VERSION, "action_ship", decorators=[])

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_ship",
            odoo_version=TEST_VERSION,
            to_version=ALT_VERSION,
            _driver=driver,
        )

        assert "Method version diff" in result
        assert "deleted" in result.lower() or "not found" in result.lower()

        _clean_version(driver, ALT_VERSION)

    def test_diff_method_added_in_newer(self, clean_neo4j):
        """Method present only in to_version is reported as added."""
        driver = clean_neo4j
        _clean_version(driver, ALT_VERSION)

        # Only to_version seeded — from_version has no such method
        _seed_method(driver, ALT_VERSION, "action_new_flow",
                     model_name="sale.order", decorators=[])

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_new_flow",
            odoo_version=TEST_VERSION,
            to_version=ALT_VERSION,
            _driver=driver,
        )

        assert "Method version diff" in result
        assert "added" in result.lower() or "not in" in result.lower()

        _clean_version(driver, ALT_VERSION)

    def test_diff_signature_changed(self, clean_neo4j):
        """Both versions present with different signatures shows both in output."""
        driver = clean_neo4j
        _clean_version(driver, ALT_VERSION)

        _seed_method(driver, TEST_VERSION, "action_confirm",
                     signature="self, vals", decorators=[])
        _seed_method(driver, ALT_VERSION, "action_confirm",
                     signature="self, vals_list", decorators=[])

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_confirm",
            odoo_version=TEST_VERSION,
            to_version=ALT_VERSION,
            _driver=driver,
        )

        assert "Method version diff" in result
        assert "self, vals" in result
        assert "self, vals_list" in result

        _clean_version(driver, ALT_VERSION)

    def test_diff_same_version_returns_single_view(self, clean_neo4j):
        """When to_version == odoo_version, single-version mode runs (backward compat)."""
        driver = clean_neo4j
        _seed_method(driver, TEST_VERSION, "action_confirm",
                     decorators=["api.model"], signature="self")

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_confirm",
            odoo_version=TEST_VERSION,
            to_version=TEST_VERSION,   # same → single-version mode
            _driver=driver,
        )

        # Single-version output has "Convention:" and "Override chain"
        assert "Convention:" in result
        assert "Override chain" in result
        # Cross-version diff header must NOT appear
        assert "Method version diff" not in result

    def test_diff_signature_null_graceful(self, clean_neo4j):
        """Null signature (pre-reindex data) outputs hint, does not crash."""
        driver = clean_neo4j
        _clean_version(driver, ALT_VERSION)

        # Seed both versions with signature=None (simulate pre-reindex)
        _seed_method(driver, TEST_VERSION, "action_confirm",
                     signature=None, decorators=[])
        _seed_method(driver, ALT_VERSION, "action_confirm",
                     signature=None, decorators=[])

        from src.mcp.server import _find_override_point
        result = _find_override_point(
            "sale.order", "action_confirm",
            odoo_version=TEST_VERSION,
            to_version=ALT_VERSION,
            _driver=driver,
        )

        # Must not crash and must contain the hint text
        assert isinstance(result, str)
        assert "not stored" in result or "run" in result.lower()

        _clean_version(driver, ALT_VERSION)
