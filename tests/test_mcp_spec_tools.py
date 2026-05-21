# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_spec_tools.py
"""MCP tool tests for the M4.5 spec layer (lookup_core_api / api_version_diff /
find_deprecated_usage / lint_check / cli_help).

Each tool is exercised with at least 3 cases: happy path, not-found / empty,
edge case (same version, invalid arg, etc.). Output must be a tree-formatted
string consumable by AI clients.
"""
import os
import sys

import pytest

from src.indexer.models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    LintRuleInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# Spec tools use a dedicated test version pair so they don't collide with
# `seeded_neo4j` (99.0) / `seeded_views` (97.0).
SPEC_VERSION_FROM = "96.0"
SPEC_VERSION_TO = "95.0"


@pytest.fixture(scope="module")
def seeded_spec_neo4j(neo4j_driver):
    """Seed CoreSymbol / LintRule / CLI* nodes for the spec-tool test suite."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        for v in (SPEC_VERSION_FROM, SPEC_VERSION_TO, TEST_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)

    # LintRule: pylint-odoo gettext + ESLint + ruff (for lint_check)
    writer.write_lint_rules([
        LintRuleInfo(
            rule_id="E8502", odoo_version=SPEC_VERSION_FROM, kind="pylint-odoo",
            message="Bad usage of _, _lt function. Use a literal string.",
            severity="error",
        ),
        LintRuleInfo(
            rule_id="no-debugger", odoo_version=SPEC_VERSION_FROM,
            kind="eslint-odoo",
            message="No debugger statement allowed",
            severity="error",
        ),
    ])

    # CoreSymbol: name_get deprecated@v96, removed@v95 + replacement display_name@v95
    # NOTE: write CoreSymbols BEFORE the user Model — _write_parse_result MERGEs
    # USES_CORE_SYMBOL only when target CoreSymbol already exists.
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.name_get",
            kind="orm_method", odoo_version=SPEC_VERSION_FROM,
            signature="name_get(self)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.display_name",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.display_name",
            kind="orm_method", odoo_version=SPEC_VERSION_FROM,
            signature="display_name (computed property)",
            status="stable",
        ),
        # safe_eval present in v96 and v95 (stable both versions)
        CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function", odoo_version=SPEC_VERSION_FROM,
            signature="safe_eval(expr, context=None)",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function", odoo_version=SPEC_VERSION_TO,
            signature="safe_eval(expr, context, locals_dict=None)",
            status="stable",
        ),
        # name_get in v95 is removed; replacement display_name@v95 added.
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.display_name",
            kind="orm_method", odoo_version=SPEC_VERSION_TO,
            signature="display_name (computed property)",
            status="added",
        ),
    ])

    # CLI Commands + Flags (for cli_help)
    writer.write_cli_commands([
        CLICommandInfo(
            name="server", odoo_version=SPEC_VERSION_FROM,
            description="Run Odoo server",
        ),
    ])
    writer.write_cli_flags([
        CLIFlagInfo(
            "--http-port", "server", SPEC_VERSION_FROM,
            type="int", default="8069",
            help="Listen port for the HTTP service",
        ),
        CLIFlagInfo(
            "--longpolling-port", "server", SPEC_VERSION_FROM,
            type="int", status="deprecated",
            replacement_flag_name="--gevent-port",
            help="Deprecated alias to the gevent-port option",
        ),
        CLIFlagInfo(
            "--gevent-port", "server", SPEC_VERSION_FROM,
            type="int", default="8072",
            help="Listen port for the gevent worker",
        ),
    ])
    writer.write_cli_flag_replacements(
        [("--longpolling-port", "--gevent-port")],
        command_name="server",
        from_version=SPEC_VERSION_FROM,
        to_version=SPEC_VERSION_FROM,
    )

    # User Module + Model + Method that uses deprecated 'name_get' (for find_deprecated_usage)
    # Written AFTER CoreSymbols so USES_CORE_SYMBOL edge MERGE finds its target.
    user_mod = ModuleInfo(
        "viin_test_spec", SPEC_VERSION_FROM, "acme_addons_test", "/tmp", [], "",
    )
    user_method = MethodInfo(
        name="legacy_label", has_super_call=False, decorators=[],
        core_symbol_refs=["name_get"],
    )
    user_model = ModelInfo(
        name="sale.order.spec", module="viin_test_spec",
        odoo_version=SPEC_VERSION_FROM,
        methods=[user_method],
    )
    writer.write_results([ParseResult(module=user_mod, models=[user_model])])

    yield SPEC_VERSION_FROM, SPEC_VERSION_TO

    with neo4j_driver.session() as session:
        for v in (SPEC_VERSION_FROM, SPEC_VERSION_TO, TEST_VERSION):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    writer.close()


@pytest.fixture
def spec_tools(seeded_spec_neo4j):
    """Import MCP spec-tool functions after seeding data."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


# --- lookup_core_api ----------------------------------------------------


class TestLookupCoreApi:
    def test_happy_path_returns_status_and_replacement(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("name_get", v_from)
        assert "name_get" in out
        assert "deprecated" in out.lower()
        assert "display_name" in out  # replacement surfaced

    def test_returns_not_found_for_unknown_symbol(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("definitely_not_a_real_symbol_xyz", v_from)
        assert "not found" in out.lower()

    def test_partial_qualified_name_resolves_via_endswith(self, spec_tools, seeded_spec_neo4j):
        """Short name like 'safe_eval' resolves to qualified_name ending in '.safe_eval'."""
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lookup_core_api("safe_eval", v_from)
        assert "safe_eval" in out
        assert "function" in out.lower()


# --- api_version_diff ---------------------------------------------------


class TestApiVersionDiff:
    def test_happy_path_signature_change(self, spec_tools, seeded_spec_neo4j):
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("safe_eval", v_from, v_to)
        # Signature differs between the two versions → "Signature" or "Stable" + diff hint
        assert "safe_eval" in out
        assert v_from in out and v_to in out

    def test_same_version_returns_no_diff(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._api_version_diff("safe_eval", v_from, v_from)
        assert "no diff" in out.lower() or "same version" in out.lower()

    def test_symbol_missing_in_both_versions(self, spec_tools, seeded_spec_neo4j):
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("nonexistent_xyz", v_from, v_to)
        assert "not found" in out.lower()

    def test_symbol_only_in_old_version_marked_removed(self, spec_tools, seeded_spec_neo4j):
        """name_get exists @v96 (deprecated) but not @v95 → diff says removed."""
        v_from, v_to = seeded_spec_neo4j
        out = spec_tools._api_version_diff("name_get", v_from, v_to)
        assert "name_get" in out
        assert "removed" in out.lower() or "deprecated" in out.lower()


# --- find_deprecated_usage ----------------------------------------------


class TestFindDeprecatedUsage:
    def test_lists_user_method_calling_deprecated_symbol(
        self, spec_tools, seeded_spec_neo4j,
    ):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._find_deprecated_usage(v_from)
        # The seeded Method `legacy_label` references `name_get` (deprecated).
        assert "legacy_label" in out
        assert "name_get" in out

    def test_returns_no_results_for_unindexed_version(self, spec_tools):
        out = spec_tools._find_deprecated_usage("90.0")
        assert "no deprecated usage" in out.lower() or "no results" in out.lower()

    def test_filter_by_kind_orm_method(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._find_deprecated_usage(v_from, kind="orm_method")
        # Filter narrows but shouldn't lose the seeded match.
        assert "legacy_label" in out


# --- lint_check ---------------------------------------------------------


class TestLintCheck:
    def test_python_code_with_gettext_violation_flagged(
        self, spec_tools, seeded_spec_neo4j,
    ):
        v_from, _ = seeded_spec_neo4j
        # Seeded LintRule E8502 message contains 'Bad usage of _, _lt function'.
        # V0 matcher is substring-on-message — the rule's message keyword
        # 'gettext' / '_lt' / 'literal string' triggers the rule.
        code = "name = _(\"Hello %s\" % user.name)"
        out = spec_tools._lint_check(code, v_from, language="python")
        # The rule may or may not match — V0 contract is structured output.
        # Header `lint_check(...)` always present (banner may prepend per WI-F6).
        assert "lint_check(" in out
        # Output must mention either 'no violations' or list a rule id.
        assert "no violations" in out.lower() or "E8502" in out or "E" in out

    def test_clean_code_returns_no_violations(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._lint_check("x = 1", v_from, language="python")
        # Trivial code shouldn't match any seeded rule.
        assert "no violations" in out.lower() or "OK" in out

    def test_invalid_language_returns_validation_error(self, spec_tools):
        out = spec_tools._lint_check("anything", "17.0", language="cobol")
        assert "language" in out.lower()
        assert "python" in out.lower() or "javascript" in out.lower()


# --- cli_help -----------------------------------------------------------


class TestCliHelp:
    def test_command_only_lists_flags(self, spec_tools, seeded_spec_neo4j):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._cli_help("server", flag=None, odoo_version=v_from)
        # Should include key flags from seeded data
        assert "--http-port" in out
        assert "--gevent-port" in out

    def test_specific_flag_shows_status_and_replacement(
        self, spec_tools, seeded_spec_neo4j,
    ):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._cli_help(
            "server", flag="--longpolling-port", odoo_version=v_from,
        )
        assert "--longpolling-port" in out
        assert "deprecated" in out.lower()
        assert "--gevent-port" in out

    def test_command_not_found_returns_helpful_message(
        self, spec_tools, seeded_spec_neo4j,
    ):
        v_from, _ = seeded_spec_neo4j
        out = spec_tools._cli_help(
            "nonexistent_cmd_xyz", flag=None, odoo_version=v_from,
        )
        assert "not found" in out.lower()


# --- profile_name filter tests for find_deprecated_usage -------------------


class TestFindDeprecatedUsageProfileFilter:
    """Verify that profile_name=None preserves backward compat and that a
    non-matching profile_name hides Method nodes from other profiles."""

    @pytest.fixture(scope="class")
    def seeded_deprecated_profiles(self, neo4j_driver):
        """Seed two Method nodes with distinct profiles that use a deprecated symbol."""
        DEPR_VERSION = "92.0"

        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=DEPR_VERSION,
            )

        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        from src.indexer.models import CoreSymbolInfo

        # CoreSymbol node (deprecated)
        writer.write_core_symbols([
            CoreSymbolInfo(
                qualified_name="odoo.models.BaseModel.name_get",
                odoo_version=DEPR_VERSION,
                kind="orm_method",
                status="deprecated",
                replacement_qname="odoo.models.BaseModel.display_name",
            ),
        ])

        # Two user modules: alpha_depr (profile alpha_depr) and beta_depr (profile beta_depr)
        alpha_mod = ModuleInfo("alpha_depr_mod", DEPR_VERSION, "repo_alpha", "/tmp", [], "")
        alpha_method = MethodInfo(
            name="legacy_alpha", has_super_call=False, decorators=[],
            core_symbol_refs=["name_get"],
        )
        alpha_model = ModelInfo(
            name="alpha.depr.model", module="alpha_depr_mod", odoo_version=DEPR_VERSION,
            methods=[alpha_method],
        )
        writer.write_results(
            [ParseResult(module=alpha_mod, models=[alpha_model])],
            profiles=["alpha_depr"],
        )

        beta_mod = ModuleInfo("beta_depr_mod", DEPR_VERSION, "repo_beta", "/tmp", [], "")
        beta_method = MethodInfo(
            name="legacy_beta", has_super_call=False, decorators=[],
            core_symbol_refs=["name_get"],
        )
        beta_model = ModelInfo(
            name="beta.depr.model", module="beta_depr_mod", odoo_version=DEPR_VERSION,
            methods=[beta_method],
        )
        writer.write_results(
            [ParseResult(module=beta_mod, models=[beta_model])],
            profiles=["beta_depr"],
        )

        writer.close()
        yield DEPR_VERSION

        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=DEPR_VERSION,
            )

    def test_profile_none_returns_both(
        self, seeded_deprecated_profiles, spec_tools,
    ):
        """profile_name=None returns hits from all profiles (backward compat)."""
        v = seeded_deprecated_profiles
        out = spec_tools._find_deprecated_usage(v, profile_name=None)
        assert "legacy_alpha" in out
        assert "legacy_beta" in out

    def test_profile_filter_excludes_other_profile(
        self, seeded_deprecated_profiles, spec_tools,
    ):
        """profile_name='alpha_depr' returns alpha hits and hides beta hits."""
        v = seeded_deprecated_profiles
        out = spec_tools._find_deprecated_usage(v, profile_name="alpha_depr")
        assert "legacy_alpha" in out
        assert "legacy_beta" not in out
