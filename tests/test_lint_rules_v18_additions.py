# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_rules_v18_additions.py
"""issue #117 bug#5 — the new v18 lint rules are served end-to-end by lint_check.

Adds W8193 (`oe_chatter` div -> `<chatter/>`), W8205 (always-invisible field needs
an XML comment), W8213 (QUnit -> Hoot) to lint_rules_18.0.json. This guards that
they are not only schema-valid (test_spec_data_lint_rules_curated) but actually
INDEXED and matched by the lint_check resolver:
  - W8213 fires through the javascript path (kind=eslint) on `QUnit.` code.
  - W8193 fires through the deterministic `oe_chatter` code_pattern.

The real v18 JSON content is the SSOT under test; rules are relabelled onto a
disposable test version so the assertions never collide with real v18 data.
"""
import dataclasses
import os
import sys

import pytest

from src.indexer.parser_lint_rules import parse_lint_rules_for_version
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

SOURCE_VERSION = "18.0"
# Unique disposable test version (issue #117 block 68-72) so module-scoped
# seed/teardown never collides with a sibling fixture (e.g. test_diff_engine_lifecycle
# also seeds 91.0); not in _FORBIDDEN_VERSIONS.
PARITY_VERSION = "71.0"


@pytest.fixture(scope="module")
def seeded_lint(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    rules = [
        dataclasses.replace(r, odoo_version=PARITY_VERSION)
        for r in parse_lint_rules_for_version(SOURCE_VERSION)
    ]
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PARITY_VERSION)
    writer.write_lint_rules(rules)
    writer.write_spec_metadata(
        kind="lint", odoo_version=PARITY_VERSION, curate_status="complete",
    )
    yield {r.rule_id for r in rules}
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PARITY_VERSION)
    writer.close()


@pytest.fixture
def spec_tools(seeded_lint):
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


class TestV18LintAdditionsServed:
    def test_new_rules_are_indexed(self, seeded_lint):
        """All three new rule ids made it into the indexed rule set."""
        for rid in ("W8193", "W8205", "W8213"):
            assert rid in seeded_lint, f"{rid} missing from indexed v18 lint rules"

    def test_qunit_to_hoot_flagged_in_js(self, spec_tools):
        """W8213 fires on QUnit usage through the javascript lint path."""
        code = "import { test } from '@odoo/hoot';\nQUnit.test('legacy', () => {});\n"
        out = spec_tools._lint_check(code, PARITY_VERSION, language="javascript")
        assert "W8213" in out, f"QUnit->Hoot rule not surfaced:\n{out}"

    def test_oe_chatter_pattern_fires(self, spec_tools):
        """W8193's deterministic `oe_chatter` code_pattern is applied by the matcher."""
        code = '<div class="oe_chatter">stuff</div>'
        out = spec_tools._lint_check(code, PARITY_VERSION, language="python")
        assert "W8193" in out, f"oe_chatter chatter rule not surfaced:\n{out}"
