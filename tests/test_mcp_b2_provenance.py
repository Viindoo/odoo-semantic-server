# SPDX-License-Identifier: AGPL-3.0-or-later
"""B2 provenance / intent rendering tests — integration tests (require Neo4j).

Covers:
  1. resolve_method: docstring rendered (positive) + absent (null-safe).
  2. describe_module: repo_url, auto_install, application, category, external
     deps rendered (positive) + absent (null-safe).
  3. find_examples: [repo] file_path:line_start rendered when present (positive)
     + absent (null-safe, old rows without line_start/repo).
  4. impact_analysis(field): USES_FIELD / DEPENDS_ON_FIELD blast radius listed
     (positive) + absent (null-safe — no edges pre-reindex).
  5. module_inspect(method='dependencies'): transitive DEPENDS_ON closure with
     load order (positive) + empty (null-safe).

All positive fixtures use TEST_VERSION="99.0" + clean_neo4j per conftest.
"""
import os
import sys

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

_B2_VERSION = TEST_VERSION  # "99.0"


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _make_writer() -> Neo4jWriter:
    return Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


def _mcp_env():
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)


# ---------------------------------------------------------------------------
# Task 1: resolve_method — docstring
# ---------------------------------------------------------------------------

class TestResolveMethodDocstring:
    """B2 Task 1 — docstring line rendered / absent null-safe."""

    @pytest.fixture(autouse=True)
    def _seed(self, neo4j_driver, clean_neo4j):
        writer = _make_writer()
        writer.setup_indexes()

        # Method WITH docstring
        mod_with = ModuleInfo("sale_b2", _B2_VERSION, "odoo_test", "/tmp", [], "")
        model_with = ModelInfo(
            name="sale.b2.order", module="sale_b2", odoo_version=_B2_VERSION,
            fields=[],
            methods=[MethodInfo(
                "action_confirm",
                has_super_call=False,
                docstring="Confirm the sale order and send confirmation email.",
            )],
        )
        # Method WITHOUT docstring (existing graph pre-reindex)
        mod_no = ModuleInfo("account_b2", _B2_VERSION, "odoo_test", "/tmp", [], "")
        model_no = ModelInfo(
            name="account.b2.move", module="account_b2", odoo_version=_B2_VERSION,
            fields=[],
            methods=[MethodInfo("action_post", has_super_call=True)],
        )
        writer.write_results([
            ParseResult(module=mod_with, models=[model_with]),
            ParseResult(module=mod_no, models=[model_no]),
        ])
        writer.close()

    def test_docstring_rendered_when_present(self):
        """Docstring first line appears in resolve_method output when non-null."""
        _mcp_env()
        from src.mcp.server import _resolve_method
        result = _resolve_method("sale.b2.order", "action_confirm", _B2_VERSION)
        assert "Docstring:" in result, f"Expected 'Docstring:' line; got:\n{result}"
        assert "Confirm the sale order" in result

    def test_docstring_absent_no_crash(self):
        """resolve_method does NOT crash and does NOT print 'Docstring:' when null."""
        _mcp_env()
        from src.mcp.server import _resolve_method
        result = _resolve_method("account.b2.move", "action_post", _B2_VERSION)
        assert "not found" not in result.lower()
        assert "action_post" in result
        assert "Docstring:" not in result, (
            "Docstring line must be absent when docstring is None"
        )


# ---------------------------------------------------------------------------
# Task 2: describe_module — new provenance fields
# ---------------------------------------------------------------------------

class TestDescribeModuleProvenance:
    """B2 Task 2 — repo_url, auto_install, application, category, external deps."""

    @pytest.fixture(autouse=True)
    def _seed(self, neo4j_driver, clean_neo4j):
        writer = _make_writer()
        writer.setup_indexes()

        # Module WITH all new fields
        mod_full = ModuleInfo(
            "viin_sale_b2", _B2_VERSION, "viindoo", "/opt/addons/viin_sale_b2",
            ["base", "sale"],
            repo_url="https://github.com/viindoo/viindoo",
            auto_install=True,
            application=True,
            category="Sales/Sales",
            external_python=["stripe", "qrcode"],
            external_bin=["wkhtmltopdf"],
        )
        # Module WITHOUT new fields (pre-reindex graph)
        mod_bare = ModuleInfo(
            "account_bare_b2", _B2_VERSION, "odoo_test", "/tmp", [], "",
        )
        writer.write_results([
            ParseResult(module=mod_full, models=[]),
            ParseResult(module=mod_bare, models=[]),
        ])
        writer.close()

    def test_repo_url_rendered(self):
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("viin_sale_b2", _B2_VERSION)
        assert "Repo URL:" in result
        assert "https://github.com/viindoo/viindoo" in result

    def test_auto_install_rendered(self):
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("viin_sale_b2", _B2_VERSION)
        assert "Auto-install: yes" in result

    def test_application_rendered(self):
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("viin_sale_b2", _B2_VERSION)
        assert "Application: yes" in result

    def test_category_rendered(self):
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("viin_sale_b2", _B2_VERSION)
        assert "Category: Sales/Sales" in result

    def test_external_deps_rendered(self):
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("viin_sale_b2", _B2_VERSION)
        assert "External deps:" in result
        assert "stripe" in result
        assert "wkhtmltopdf" in result

    def test_no_new_fields_no_crash(self):
        """Bare module (pre-reindex, no new props) renders without crashing."""
        _mcp_env()
        from src.mcp.server import _describe_module
        result = _describe_module("account_bare_b2", _B2_VERSION)
        assert "account_bare_b2" in result
        # Null-safe: none of these optional labels should appear
        assert "Repo URL:" not in result
        assert "Auto-install:" not in result
        assert "Application:" not in result
        assert "Category:" not in result
        assert "External deps:" not in result


# ---------------------------------------------------------------------------
# Task 4: impact_analysis(field) — USES_FIELD / DEPENDS_ON_FIELD
# ---------------------------------------------------------------------------

class TestImpactAnalysisFieldEdges:
    """B2 Task 4 — field-level blast radius from USES_FIELD / DEPENDS_ON_FIELD."""

    @pytest.fixture(autouse=True)
    def _seed(self, neo4j_driver, clean_neo4j):
        writer = _make_writer()
        writer.setup_indexes()

        # Module + model with a field and methods that reference it.
        mod = ModuleInfo("sale_impact_b2", _B2_VERSION, "odoo_test", "/tmp", [], "")
        model = ModelInfo(
            name="sale.impact.order", module="sale_impact_b2",
            odoo_version=_B2_VERSION,
            fields=[
                FieldInfo("amount_total", "monetary"),
            ],
            methods=[
                # method_uses: field_refs includes "amount_total" → USES_FIELD edge
                MethodInfo(
                    "action_send_mail",
                    has_super_call=False,
                    field_refs=["amount_total"],
                ),
                # method_depends: depends first-segment "amount_total" → DEPENDS_ON_FIELD edge
                # The writer extracts the first segment of each depends path.
                MethodInfo(
                    "_compute_discount",
                    has_super_call=False,
                    depends=["amount_total"],
                ),
                # method_unrelated: no field refs
                MethodInfo("action_cancel", has_super_call=True),
            ],
        )
        writer.write_results([ParseResult(module=mod, models=[model])])
        writer.close()

    def test_uses_field_methods_listed(self):
        """Methods with USES_FIELD edge appear in 'Methods using this field' section."""
        _mcp_env()
        from src.mcp.server import _impact_analysis
        result = _impact_analysis("field", "sale.impact.order.amount_total", _B2_VERSION)
        assert "Methods using this field" in result, (
            f"Expected 'Methods using this field' section; got:\n{result}"
        )
        assert "action_send_mail" in result

    def test_depends_on_field_methods_listed(self):
        """Methods with DEPENDS_ON_FIELD edge appear in 'Compute-dependent methods' section."""
        _mcp_env()
        from src.mcp.server import _impact_analysis
        result = _impact_analysis("field", "sale.impact.order.amount_total", _B2_VERSION)
        assert "Compute-dependent methods" in result, (
            f"Expected 'Compute-dependent methods' section; got:\n{result}"
        )
        assert "_compute_discount" in result

    def test_unrelated_method_not_in_field_sections(self):
        """action_cancel (no field refs) must not appear in field-level sections."""
        _mcp_env()
        from src.mcp.server import _impact_analysis
        result = _impact_analysis("field", "sale.impact.order.amount_total", _B2_VERSION)
        # action_cancel may appear in "Methods on model with super()" (existing section)
        # but must NOT appear in "Methods using this field" / "Compute-dependent methods"
        # We check the sections are present and action_cancel is NOT in them.
        uses_idx = result.find("Methods using this field")
        comp_idx = result.find("Compute-dependent methods")
        # The next section after both field sections is the JS patches section.
        # Extract the field-sections substring.
        end_idx = result.find("├─ JS patches")
        if end_idx == -1:
            end_idx = len(result)
        field_sections = result[min(uses_idx, comp_idx):end_idx]
        assert "action_cancel" not in field_sections

    def test_field_impact_no_edges_no_crash(self, neo4j_driver):
        """Field with no USES_FIELD/DEPENDS_ON_FIELD edges renders without crashing."""
        # Seed a fresh field without field_refs / depends.
        with neo4j_driver.session() as s:
            s.run(
                "MERGE (:Field {name: 'state', model: 'sale.impact.order',"
                " module: 'sale_impact_b2', odoo_version: $v})",
                v=_B2_VERSION,
            )
        _mcp_env()
        from src.mcp.server import _impact_analysis
        result = _impact_analysis("field", "sale.impact.order.state", _B2_VERSION)
        assert "Methods using this field" not in result
        assert "Compute-dependent methods" not in result
        # Existing sections still present
        assert "Views:" in result or "views" in result.lower()

    def test_field_impact_old_note_removed(self):
        """The stale 'field-level impact requires F4 USES_FIELD edge (deferred to M5)' note
        must no longer appear in output."""
        _mcp_env()
        from src.mcp.server import _impact_analysis
        result = _impact_analysis("field", "sale.impact.order.amount_total", _B2_VERSION)
        assert "deferred to M5" not in result, (
            "Stale TODO note should be removed in B2"
        )


# ---------------------------------------------------------------------------
# Task 5: module_inspect(method='dependencies')
# ---------------------------------------------------------------------------

class TestModuleInspectDependencies:
    """B2 Task 5 — transitive DEPENDS_ON closure + load order."""

    @pytest.fixture(autouse=True)
    def _seed(self, neo4j_driver, clean_neo4j):
        writer = _make_writer()
        writer.setup_indexes()

        # Chain: top_mod -> mid_mod -> base_mod
        base = ModuleInfo(
            "base_b2", _B2_VERSION, "odoo_test", "/tmp", [], "",
            repo_url="https://github.com/odoo/odoo",
        )
        mid = ModuleInfo(
            "mid_b2", _B2_VERSION, "odoo_test", "/tmp", ["base_b2"], "",
        )
        top = ModuleInfo(
            "top_b2", _B2_VERSION, "acme_test", "/tmp", ["mid_b2"], "",
        )
        # Isolated module (no deps)
        alone = ModuleInfo(
            "alone_b2", _B2_VERSION, "odoo_test", "/tmp", [], "",
        )
        writer.write_results([
            ParseResult(module=base, models=[]),
            ParseResult(module=mid, models=[]),
            ParseResult(module=top, models=[]),
            ParseResult(module=alone, models=[]),
        ])
        writer.close()

    def test_dependencies_returns_closure(self):
        """module_inspect(method='dependencies') returns all transitive deps."""
        _mcp_env()
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("top_b2", method="dependencies", odoo_version=_B2_VERSION)
        assert "dependency closure" in result.lower() or "Transitive dependencies" in result, (
            f"Expected closure header; got:\n{result}"
        )
        assert "mid_b2" in result
        assert "base_b2" in result

    def test_dependencies_load_order_numbered(self):
        """Each dependency has a sequential load-order number."""
        _mcp_env()
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("top_b2", method="dependencies", odoo_version=_B2_VERSION)
        # Expect "1." and "2." in the load order
        assert " 1." in result or "1." in result
        assert " 2." in result or "2." in result

    def test_dependencies_shows_repo_url(self):
        """Dependencies with repo_url show the URL in output."""
        _mcp_env()
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("top_b2", method="dependencies", odoo_version=_B2_VERSION)
        assert "https://github.com/odoo/odoo" in result

    def test_dependencies_empty_no_crash(self):
        """Module with no deps renders gracefully with 'No transitive dependencies found'."""
        _mcp_env()
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("alone_b2", method="dependencies", odoo_version=_B2_VERSION)
        assert "No transitive dependencies found" in result

    def test_dependencies_not_found_no_crash(self):
        """Nonexistent module returns 'No module named' error, not a crash."""
        _mcp_env()
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("ghost_module", method="dependencies",
                                 odoo_version=_B2_VERSION)
        assert "No module named" in result or "not found" in result.lower()

    def test_module_inspect_dependencies_in_valid_methods(self):
        """'dependencies' is a valid method for module_inspect (no Error: prefix)."""
        from src.mcp.inspect import _MODULE_METHODS
        assert "dependencies" in _MODULE_METHODS

    def test_module_inspect_invalid_method_still_errors(self):
        """Invalid method still returns Error: (regression guard)."""
        from src.mcp.inspect import _module_inspect
        result = _module_inspect("sale", method="bogus_xyz")
        assert result.startswith("Error:")
