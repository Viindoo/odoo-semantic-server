# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_smoke_pattern_wow.py
"""Per-PR smoke: M4.6 Pattern Wow pipeline against mini fixture → assert counts.

Approach A smoke tier: uses tests/fixtures/patterns_smoke.json (4 mini entries
at SMOKE_VERSION='99.0') to exercise the full
seed_patterns CLI → write_pattern_examples → Neo4j queries pipeline.

Also exercises check_module_exists EE-confusion path + find_override_point
super_ratio path against pre-seeded fixtures (no postgres / embedder needed).

Run:
    pytest tests/test_smoke_pattern_wow.py -v -m smoke

Why no suggest_pattern test here: that path requires pgvector + embedder
(covered by integration job in tests/test_mcp_pattern_tools.py).
Smoke focuses on B1 regression catch in: seed CLI, writer_neo4j PatternExample
path, EE_CONFUSION dict lookup, find_override_point Cypher + super_ratio calc.
"""
import os
from pathlib import Path

import pytest

from src.indexer.models import (
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
from src.indexer.seed_patterns import main as seed_main
from src.indexer.writer_neo4j import Neo4jWriter
from src.mcp.server import (
    _check_module_exists,
    _find_override_point,
)

pytestmark = [pytest.mark.neo4j, pytest.mark.smoke]

SMOKE_VERSION = "99.0"
FIXTURE_FILE = Path(__file__).parent / "fixtures" / "patterns_smoke.json"


def _purge_smoke(session) -> None:
    """Delete every node tagged with SMOKE_VERSION + the seeded PatternExamples.

    PatternExample uses pattern_id as its composite key (no odoo_version
    component), so we purge by id prefix 'smoke-' instead of by version.
    """
    session.run(
        "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
        v=SMOKE_VERSION,
    )
    session.run(
        "MATCH (p:PatternExample) WHERE p.pattern_id STARTS WITH 'smoke-' "
        "DETACH DELETE p",
    )


@pytest.fixture(autouse=True)
def _seed_env(monkeypatch):
    """Bridge test env (NEO4J_TEST_*) → seed_patterns CLI env (NEO4J_*).

    seed_patterns._write_neo4j reads NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD via
    src.config; tests/CI provide credentials under NEO4J_TEST_* prefix so the
    real configured server (~/.config/odoo-semantic.conf) stays untouched.
    """
    monkeypatch.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
    )
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


@pytest.fixture(scope="module")
def smoke_writer(neo4j_driver):
    """Module-scoped Neo4jWriter; purge SMOKE_VERSION + smoke- patterns
    on setup AND teardown — guarantees isolation even on mid-test failure."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        _purge_smoke(session)
    yield writer
    with neo4j_driver.session() as session:
        _purge_smoke(session)
    writer.close()


# ---------------------------------------------------------------------------
# Test 1 — seed_patterns CLI E2E: fixture → 4 PatternExample nodes in Neo4j
# ---------------------------------------------------------------------------


class TestSmokeSeedPipeline:
    """seed_patterns.py main() runs end-to-end against fixture (no embed)."""

    def test_seed_cli_no_embed_succeeds(self, smoke_writer, neo4j_driver):
        """seed_patterns.main(['--no-embed', '--patterns-file', fixture]) → exit 0
        and Neo4j has 4 PatternExample nodes."""
        rc = seed_main([
            "--no-embed",
            "--version", SMOKE_VERSION,
            "--patterns-file", str(FIXTURE_FILE),
        ])
        assert rc == 0, f"seed_patterns CLI exited {rc} (expected 0)"

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (p:PatternExample) "
                "WHERE p.pattern_id STARTS WITH 'smoke-' "
                "RETURN count(p) AS c",
            ).single()
        assert row["c"] == 4, (
            f"Expected 4 smoke PatternExample nodes, got {row['c']}. "
            "Check seed_patterns.main or write_pattern_examples."
        )

    def test_seed_idempotent_rerun(self, smoke_writer, neo4j_driver):
        """Second run of same fixture → count unchanged (MERGE on pattern_id)."""
        rc = seed_main([
            "--no-embed",
            "--version", SMOKE_VERSION,
            "--patterns-file", str(FIXTURE_FILE),
        ])
        assert rc == 0

        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (p:PatternExample) "
                "WHERE p.pattern_id STARTS WITH 'smoke-' "
                "RETURN count(p) AS c",
            ).single()
        assert row["c"] == 4, (
            f"Idempotency broken: expected 4, got {row['c']}. "
            "MERGE key in write_pattern_examples may be wrong."
        )

    def test_uses_core_symbol_silent_skip(self, smoke_writer, neo4j_driver):
        """Pattern with core_symbol_names=['odoo.api.depends'] but no CoreSymbol
        seeded → no edge created, no error (per ADR-0003 §5 graceful skip).

        Re-uses the seeded data from previous tests in this class.
        """
        with neo4j_driver.session() as s:
            row = s.run(
                "MATCH (p:PatternExample {pattern_id: 'smoke-computed-field'})"
                "-[:USES_CORE_SYMBOL]->() "
                "RETURN count(*) AS c",
            ).single()
        assert row["c"] == 0, (
            f"Expected 0 USES_CORE_SYMBOL edges (no CoreSymbol seeded for "
            f"v{SMOKE_VERSION}), got {row['c']}. Silent-skip path broken."
        )


# ---------------------------------------------------------------------------
# Test 2 — check_module_exists EE-confusion path
# ---------------------------------------------------------------------------


class TestSmokeCheckModuleExists:
    """EE_CONFUSION dict + Module.edition smoke."""

    def test_ee_confusion_warns_on_knowledge(
        self, smoke_writer, neo4j_driver,
    ):
        """check_module_exists('knowledge', SMOKE_VERSION) → output flags EE
        and tells user not to depend on it (Viindoo equivalent = None)."""
        out = _check_module_exists(
            "knowledge", odoo_version=SMOKE_VERSION, _driver=neo4j_driver,
        )
        assert "knowledge" in out
        assert "Yes" in out, (
            f"Expected EE confusion 'Yes' flag, got:\n{out}"
        )
        assert "Do NOT" in out or "Enterprise" in out, (
            f"Expected EE warning text, got:\n{out}"
        )

    def test_indexed_viindoo_module_recognized(
        self, smoke_writer, neo4j_driver,
    ):
        """Seed 1 viin_* module → check_module_exists shows edition=viindoo."""
        viin_mod = ModuleInfo(
            name="smoke_viin_helpdesk", odoo_version=SMOKE_VERSION,
            repo="acme_addons17", path="/p/smoke_viin_helpdesk",
            depends=[], version_raw="", edition="viindoo",
        )
        smoke_writer.write_results([
            ParseResult(module=viin_mod, models=[]),
        ])

        out = _check_module_exists(
            "smoke_viin_helpdesk", odoo_version=SMOKE_VERSION,
            _driver=neo4j_driver,
        )
        assert "viindoo" in out.lower(), (
            f"Expected edition=viindoo in output, got:\n{out}"
        )


# ---------------------------------------------------------------------------
# Test 3 — find_override_point super_ratio + convention path
# ---------------------------------------------------------------------------


class TestSmokeFindOverridePoint:
    """Method override chain + super_ratio + convention_kind smoke."""

    def test_action_method_super_ratio_2_of_3(
        self, smoke_writer, neo4j_driver,
    ):
        """Seed 3-module override chain (1 base no-super + 2 ext with-super)
        → find_override_point reports super_ratio '2/3' + convention 'action'."""
        # Wire: same shape as test_mcp_pattern_tools.seeded_method_chain
        modules = [
            ("smoke_sale_base", "community", False),
            ("smoke_sale_ext_a", "viindoo", True),
            ("smoke_sale_ext_b", "viindoo", True),
        ]
        results = []
        for mod_name, edition, has_super in modules:
            module = ModuleInfo(
                name=mod_name, odoo_version=SMOKE_VERSION, repo="r",
                path=f"/p/{mod_name}", depends=[], version_raw="",
                edition=edition,
            )
            model = ModelInfo(
                name="smoke.sale.order", module=mod_name,
                odoo_version=SMOKE_VERSION,
                methods=[
                    MethodInfo(
                        name="action_confirm", has_super_call=has_super,
                        convention_kind="action", super_safety="always",
                        return_required=True,
                    ),
                ],
            )
            results.append(ParseResult(module=module, models=[model]))
        smoke_writer.write_results(results)

        out = _find_override_point(
            "smoke.sale.order", "action_confirm",
            odoo_version=SMOKE_VERSION, _driver=neo4j_driver,
        )
        assert "2/3" in out, (
            f"Expected super_ratio '2/3', got:\n{out}"
        )
        assert "action" in out.lower(), (
            f"Expected convention 'action' in output, got:\n{out}"
        )
        assert "always" in out.lower(), (
            f"Expected super_safety 'always' in output, got:\n{out}"
        )

    def test_method_not_found_returns_helpful_message(
        self, smoke_writer, neo4j_driver,
    ):
        """find_override_point on absent method → not found message, no crash."""
        out = _find_override_point(
            "smoke.sale.order", "nonexistent_method",
            odoo_version=SMOKE_VERSION, _driver=neo4j_driver,
        )
        assert "not found" in out.lower(), (
            f"Expected 'not found' message, got:\n{out}"
        )
