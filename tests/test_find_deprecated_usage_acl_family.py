# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_find_deprecated_usage_acl_family.py
"""issue #117 bug#2 reconcile — end-to-end find_deprecated_usage regression.

Two independent halves had to be fixed for the v18 ACL/cache rename family to be
flagged, and this test exercises BOTH through the real edge writer + tool query:

  1. parser_python._DEPRECATED_API_SYMBOLS must list the deprecated call names so
     the user Method gets ``core_symbol_refs`` (the USES_CORE_SYMBOL edge source).
  2. parser_odoo_core must index the matching CoreSymbol with status='deprecated'
     (for `_`-prefixed members, the underscore-skip fix) so the edge has a target
     — the writer only MERGEs the edge to a deprecated/removed CoreSymbol.

This test seeds the deprecated CoreSymbols + a user method referencing them and
asserts find_deprecated_usage surfaces every member. The NEW replacement
``check_access`` must NOT be reported (it is the migration target, not deprecated).
"""
import os
import sys

import pytest

from src.indexer.models import (
    CoreSymbolInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Unique disposable test version (issue #117 block 68-72); NOT 93.0, which is in
# _FORBIDDEN_VERSIONS (prior production pollution, test_pattern_seed_no_test_pollution).
VERSION = "69.0"


@pytest.fixture(scope="module")
def seeded_acl_usage(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VERSION)

    # Deprecated CoreSymbols (write BEFORE the user model so the edge MERGE finds
    # its target). Mirrors what parser_odoo_core now emits: public deprecated
    # members (check_access_rights/rule) + the `_`-prefixed aliases that the
    # underscore-skip fix unlocked (_check_recursion / _filter_access_rules).
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.check_access_rights",
            kind="orm_method", odoo_version=VERSION,
            signature="check_access_rights(self, operation, raise_exception=True)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.check_access",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel._check_recursion",
            kind="orm_method", odoo_version=VERSION,
            signature="_check_recursion(self, parent=None)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel._has_cycle",
        ),
        # The NEW replacement — present and STABLE, must never be reported.
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.check_access",
            kind="orm_method", odoo_version=VERSION,
            signature="check_access(self, operation)",
            status="stable",
        ),
    ])

    user_mod = ModuleInfo(
        "viin_acl_legacy", VERSION, "acme_addons_test", "/tmp", [], "",
    )
    user_method = MethodInfo(
        name="guard", has_super_call=False, decorators=[],
        core_symbol_refs=["check_access_rights", "_check_recursion", "check_access"],
    )
    user_model = ModelInfo(
        name="sale.order.acl", module="viin_acl_legacy",
        odoo_version=VERSION, methods=[user_method],
    )
    writer.write_results([ParseResult(module=user_mod, models=[user_model])])

    yield VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VERSION)
    writer.close()


@pytest.fixture
def spec_tools(seeded_acl_usage):
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


class TestFindDeprecatedUsageAclFamily:
    def test_public_deprecated_acl_method_flagged(self, spec_tools, seeded_acl_usage):
        """check_access_rights usage is reported (parser_python hot-list half)."""
        out = spec_tools._find_deprecated_usage(VERSION)
        assert "guard" in out
        assert "check_access_rights" in out

    def test_underscore_deprecated_alias_flagged(self, spec_tools, seeded_acl_usage):
        """_check_recursion usage is reported — only possible once parser_odoo_core
        stops skipping the deprecated underscore alias (bug#2)."""
        out = spec_tools._find_deprecated_usage(VERSION)
        assert "_check_recursion" in out

    def test_new_replacement_not_flagged(self, spec_tools, seeded_acl_usage):
        """check_access (the stable v18 replacement) must NOT be reported."""
        out = spec_tools._find_deprecated_usage(VERSION)
        # The deprecated alias is present, but the stable replacement is not a hit.
        # `check_access` is a prefix of `check_access_rights`, so assert on the
        # exact 'uses:' line the formatter emits per hit.
        assert "uses: check_access (" not in out
