# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_find_deprecated_usage_decorators.py
"""GAP-1 (osm-audit-orm.md) — find_deprecated_usage surfaces version-removed
method decorators (@api.multi / @api.one) stored on Method.decorators.

These decorators have NO USES_CORE_SYMBOL edge — they live as plain
'api.<attr>' strings on the Method node — so the call-based scan alone never
saw them. The fix adds a version-gated decorator leg:

  - @api.one removed in 10.0
  - @api.multi removed in 13.0

A decorator is flagged ONLY once the queried version is at or past its removal
version, so the SAME @api.multi method is a hit on v17 but NOT on v12 (where
the decorator was still a valid part of the framework). This test seeds one
Method with decorators=['api.multi'] at two disposable versions and asserts the
version gate from both directions, plus the version-removed Cypher path running
alongside the unchanged call-based path.
"""
import os
import sys

import pytest

from src.indexer.models import (
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Two disposable test versions straddling the @api.multi removal boundary (13.0).
# Neither is a real indexed Odoo version (no x.5 release exists, and 95.0 is a
# pure test sentinel), so seeding + DETACH DELETE on these versions never touches
# real data. The version gate compares float(version):
#   - VERSION_FLAGGED 95.0 >= 13.0 -> @api.multi reported
#   - VERSION_VALID   12.5 <  13.0 -> @api.multi NOT reported (still valid then)
VERSION_FLAGGED = "95.0"
VERSION_VALID = "12.5"


def _seed(neo4j_driver, version: str):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=version
        )
    user_mod = ModuleInfo(
        "viin_legacy_decorator", version, "acme_addons_test", "/tmp", [], "",
    )
    # Method carrying the @api.multi decorator string exactly as the indexer
    # stores it (parser_python emits 'api.<attr>', no leading '@').
    user_method = MethodInfo(
        name="action_confirm", has_super_call=False,
        decorators=["api.multi"], core_symbol_refs=[],
    )
    user_model = ModelInfo(
        name="sale.order.legacy", module="viin_legacy_decorator",
        odoo_version=version, methods=[user_method],
    )
    writer.write_results([ParseResult(module=user_mod, models=[user_model])])
    return writer


@pytest.fixture(scope="module")
def seeded_decorator_flagged(neo4j_driver):
    writer = _seed(neo4j_driver, VERSION_FLAGGED)
    yield VERSION_FLAGGED
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=VERSION_FLAGGED,
        )
    writer.close()


@pytest.fixture(scope="module")
def seeded_decorator_valid(neo4j_driver):
    writer = _seed(neo4j_driver, VERSION_VALID)
    yield VERSION_VALID
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=VERSION_VALID,
        )
    writer.close()


@pytest.fixture
def spec_tools():
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


class TestFindDeprecatedUsageDecorators:
    def test_api_multi_flagged_at_or_after_removal(
        self, spec_tools, seeded_decorator_flagged
    ):
        """On a version >= 13.0, a method decorated @api.multi is reported as a
        deprecated-usage hit even though it has no USES_CORE_SYMBOL edge."""
        out = spec_tools._find_deprecated_usage(VERSION_FLAGGED)
        assert "action_confirm" in out
        assert "api.multi" in out
        # The synthetic hit carries the framework-change replacement note.
        assert "multi by default" in out

    def test_api_multi_not_flagged_before_removal(
        self, spec_tools, seeded_decorator_valid
    ):
        """On a version < 13.0, @api.multi is still a valid part of the
        framework and must NOT be reported (version gate, false-positive guard)."""
        out = spec_tools._find_deprecated_usage(VERSION_VALID)
        assert "api.multi" not in out
        assert "action_confirm" not in out
