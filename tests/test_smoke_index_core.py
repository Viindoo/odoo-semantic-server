# tests/test_smoke_index_core.py
"""Per-PR smoke: index-core pipeline against mini fixture → assert counts.

Approach A smoke tier: uses tests/fixtures/odoo_core_min/ (8 mini files written
from scratch, no real Odoo GPL source) to exercise the full
parse_odoo_core → write_core_symbols → verify Neo4j counts pipeline.

Also exercises the LintRule and CLIFlag paths via spec_data/99.0 placeholders.

Run:
    pytest tests/test_smoke_index_core.py -v -m smoke
"""
import os
from pathlib import Path

import pytest

from src.indexer.models import CoreSymbolInfo
from src.indexer.parser_cli import parse_cli_flags
from src.indexer.parser_lint_rules import parse_lint_rules_for_version
from src.indexer.parser_odoo_core import parse_odoo_core
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = [pytest.mark.neo4j, pytest.mark.smoke]

SMOKE_VERSION = "99.0"  # isolated test version — never conflicts with real data
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "odoo_core_min"
# spec_data lives alongside the parser sources; default lookup finds it automatically.


@pytest.fixture(scope="module")
def smoke_writer(neo4j_driver):
    """Module-scoped Neo4jWriter for smoke tests; auto-cleans SMOKE_VERSION nodes."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SMOKE_VERSION
        )
    yield writer
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SMOKE_VERSION
        )
    writer.close()


# ---------------------------------------------------------------------------
# Test 1 — pure parse (no Neo4j): fixture produces minimum symbol set
# ---------------------------------------------------------------------------

class TestSmokeParseFixture:
    """Parse-only tests: no Neo4j required."""

    # Override pytestmark to remove neo4j requirement for this class.
    pytestmark = [pytest.mark.smoke]

    def test_parse_core_extracts_minimum_symbols(self):
        """8-file fixture → ≥25 CoreSymbols across all 6 parser-defined kinds."""
        symbols = parse_odoo_core(str(FIXTURE_ROOT), SMOKE_VERSION)
        # C4 TEMP: threshold raised to impossible value to test failure path.
        # REVERT this after verifying CI fails + issue-creation path triggers.
        assert len(symbols) >= 99999, (
            f"Expected ≥99999 symbols from fixture, got {len(symbols)}. "
            "Check fixture has content in all 8 allow-list files."
        )

    def test_parse_core_covers_all_six_kinds(self):
        """All 6 kinds the parser can produce must be present in fixture output.

        Actual kinds from parser_odoo_core._classify_class + _method_kind:
          class, field_type, exception  (from _classify_class)
          function, orm_method, cursor_method  (from _method_kind / top-level)
        """
        symbols = parse_odoo_core(str(FIXTURE_ROOT), SMOKE_VERSION)
        actual_kinds = {s.kind for s in symbols}
        expected_kinds = {
            "function",       # top-level functions (api.py, tools/safe_eval.py, tools/sql.py)
            "class",          # plain classes (Query in tools/query.py)
            "field_type",     # Field subclasses (fields.py)
            "exception",      # Exception subclasses (exceptions.py)
            "orm_method",     # methods inside BaseModel/Model/TransientModel/AbstractModel
            "cursor_method",  # methods inside classes in odoo.sql_db module
        }
        missing = expected_kinds - actual_kinds
        assert not missing, (
            f"Fixture missing kinds: {missing}. "
            f"Got: {actual_kinds}. "
            "Extend fixture files to cover all parser-defined kinds."
        )

    def test_parse_core_version_tag_correct(self):
        """Every symbol carries SMOKE_VERSION as odoo_version."""
        symbols = parse_odoo_core(str(FIXTURE_ROOT), SMOKE_VERSION)
        wrong = [s for s in symbols if s.odoo_version != SMOKE_VERSION]
        assert not wrong, (
            f"{len(wrong)} symbols have wrong version: {wrong[:3]}"
        )

    def test_lint_rules_placeholder_has_at_least_one_entry(self):
        """spec_data/lint_rules_99.0.json must have ≥1 rule (write path exercised)."""
        rules = parse_lint_rules_for_version(SMOKE_VERSION)
        assert len(rules) >= 1, (
            "spec_data/lint_rules_99.0.json is missing or empty. "
            "Add ≥1 rule entry so write_lint_rules path is exercised in smoke."
        )

    def test_cli_flags_placeholder_has_at_least_one_entry(self):
        """spec_data/cli_flags_99.0.json must have ≥1 flag (write path exercised).

        parse_cli_flags with a non-existent source_root still loads static placeholder.
        """
        flags = parse_cli_flags("/nonexistent/path", SMOKE_VERSION)
        assert len(flags) >= 1, (
            "spec_data/cli_flags_99.0.json is missing or empty. "
            "Add ≥1 flag entry so write_cli_flags path is exercised in smoke."
        )


# ---------------------------------------------------------------------------
# Test 2 — integration: full pipeline writes correct counts to Neo4j
# ---------------------------------------------------------------------------

class TestSmokeIndexCoreWritesNeo4j:
    """Integration tests: require Neo4j (pytestmark includes neo4j)."""

    def test_core_symbols_written_and_queryable(self, smoke_writer, neo4j_driver):
        """parse_odoo_core fixture → write_core_symbols → Neo4j has ≥25 CoreSymbol nodes."""
        symbols = parse_odoo_core(str(FIXTURE_ROOT), SMOKE_VERSION)
        smoke_writer.write_core_symbols(symbols)

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (cs:CoreSymbol {odoo_version: $v}) RETURN count(cs) AS c",
                v=SMOKE_VERSION,
            ).single()
        count = row["c"]
        assert count >= 25, (
            f"Neo4j CoreSymbol count {count} < 25. "
            "Check write_core_symbols or fixture content."
        )

    def test_lint_rules_written_and_queryable(self, smoke_writer, neo4j_driver):
        """spec_data/lint_rules_99.0.json → write_lint_rules → Neo4j has ≥1 LintRule."""
        rules = parse_lint_rules_for_version(SMOKE_VERSION)
        smoke_writer.write_lint_rules(rules)

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (lr:LintRule {odoo_version: $v}) RETURN count(lr) AS c",
                v=SMOKE_VERSION,
            ).single()
        assert row["c"] >= 1, (
            f"LintRule count {row['c']} = 0. B1 regression in write_lint_rules?"
        )

    def test_cli_flags_written_and_queryable(self, smoke_writer, neo4j_driver):
        """spec_data/cli_flags_99.0.json → write_cli_flags → Neo4j has ≥1 CLIFlag.

        Note: no CLICommand is written (fixture has no odoo/cli/*.py), so
        OF_COMMAND edge creation is silently skipped — that is correct behaviour.
        """
        flags = parse_cli_flags("/nonexistent/path", SMOKE_VERSION)
        smoke_writer.write_cli_flags(flags)

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (cf:CLIFlag {odoo_version: $v}) RETURN count(cf) AS c",
                v=SMOKE_VERSION,
            ).single()
        assert row["c"] >= 1, (
            f"CLIFlag count {row['c']} = 0. B1 regression in write_cli_flags?"
        )

    def test_idempotent_rerun_does_not_duplicate(self, smoke_writer, neo4j_driver):
        """Second write of the same fixture → counts unchanged (MERGE semantics)."""
        symbols = parse_odoo_core(str(FIXTURE_ROOT), SMOKE_VERSION)

        # First write already done by previous tests in this module.
        # Second write:
        smoke_writer.write_core_symbols(symbols)

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (cs:CoreSymbol {odoo_version: $v}) RETURN count(cs) AS c",
                v=SMOKE_VERSION,
            ).single()

        # Count must be the same as after first write (no duplicates).
        assert row["c"] == len(symbols), (
            f"Idempotency broken: expected {len(symbols)}, got {row['c']}. "
            "MERGE key may be wrong in write_core_symbols."
        )


# ---------------------------------------------------------------------------
# Test 3 — lifecycle: symbols from 2 fixture versions → diff properties set
# ---------------------------------------------------------------------------

_LIFECYCLE_V_OLD = "98.0"  # immediately preceding SMOKE_VERSION
_LIFECYCLE_V_NEW = SMOKE_VERSION  # 99.0


class TestSmokeLifecycleDiff:
    """Verify that lifecycle diff writes added_in on newly-appearing symbols."""

    pytestmark = [pytest.mark.neo4j, pytest.mark.smoke]

    def test_added_symbol_gets_added_in_property(self, smoke_writer, neo4j_driver):
        """Index v98 with a subset, then v99 with a superset → new symbol has added_in."""
        from src.indexer.diff_engine import compute_diff

        # Seed old version (98.0) with one symbol
        old_sym = CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function",
            odoo_version=_LIFECYCLE_V_OLD,
        )
        smoke_writer.write_core_symbols([old_sym])

        # New version (99.0) adds a second symbol
        new_sym_a = CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.safe_eval",
            kind="function",
            odoo_version=_LIFECYCLE_V_NEW,
        )
        new_sym_b = CoreSymbolInfo(
            qualified_name="odoo.tools.safe_eval.expr_eval",
            kind="function",
            odoo_version=_LIFECYCLE_V_NEW,
        )
        smoke_writer.write_core_symbols([new_sym_a, new_sym_b])

        diff = compute_diff([old_sym], [new_sym_a, new_sym_b])

        # write_lifecycle_properties sets added_in on nodes in to_version
        smoke_writer.write_lifecycle_properties(
            diff,
            from_version=_LIFECYCLE_V_OLD,
            to_version=_LIFECYCLE_V_NEW,
        )

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v}) "
                "RETURN cs.added_in AS ai",
                qn="odoo.tools.safe_eval.expr_eval",
                v=_LIFECYCLE_V_NEW,
            ).single()

        assert row is not None, "expr_eval node not found in Neo4j"
        assert row["ai"] == _LIFECYCLE_V_NEW, (
            f"added_in expected {_LIFECYCLE_V_NEW!r}, got {row['ai']!r}. "
            "write_lifecycle_properties may be broken."
        )

        # Cleanup v98 nodes (v99 cleaned by module fixture teardown)
        with neo4j_driver.session() as s:
            s.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=_LIFECYCLE_V_OLD,
            )
