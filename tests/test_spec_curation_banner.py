# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_spec_curation_banner.py
"""Integration tests for lint_check 3-tier disclosure and curation banner (WI-6/WI-8).

Tests:
1. Tier-1 hard warning: no rules indexed (or curate_status absent) -> "NOT a clean bill".
2. Tier-2 soft banner: curate_status='pending' + rules present -> "pending curation" banner.
3. Tier-3 normal: curate_status='complete' + rules -> no curation banner.
4. _cli_help output has curation banner when curate_status = 'pending'.
"""
import os

import pytest

from src.indexer.models import LintRuleInfo
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_BANNER_V = "95.0"    # pending + rules -> Tier-2 soft banner
_BANNER_V2 = "96.0"   # pending + no rules -> Tier-1 hard warning; then done
_BANNER_V3 = "97.0"   # complete + rules -> Tier-3 normal
_BANNER_V4 = "98.0"   # no metadata at all -> Tier-1 hard warning


@pytest.fixture(scope="module")
def banner_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2, _BANNER_V3, _BANNER_V4):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    yield writer
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2, _BANNER_V3, _BANNER_V4):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    writer.close()


# ---------------------------------------------------------------------------
# Tier-1: empty-index hard warning
# ---------------------------------------------------------------------------

class TestLintCheckEmptyIndexHardWarning:
    """When no rules exist (or curate_status absent), output must be a hard warning."""

    def test_no_rules_no_metadata_is_not_clean_bill(self, banner_writer, neo4j_driver):
        """Version with no LintRule and no SpecMetadata -> hard 'NOT a clean bill' warning.

        This is the Tier-1 disclosure per ADR-0002 §4: a zero-rules result is NOT
        a clean bill of health - it is a data gap, and the output must say so.

        _BANNER_V4 is a dedicated version string with no rules and no SpecMetadata seeded.
        The output must contain the explicit 'NOT a clean bill' text from
        _format_lint_empty_index. The header still says '0 violations' (tree grammar kept)
        but the warning branch makes the data-gap explicit.
        """
        import src.mcp.server as spec_tools

        out = spec_tools._lint_check("x = 1", _BANNER_V4, language="python")
        assert "not a clean bill" in out.lower(), (
            f"Expected 'NOT a clean bill' hard warning when no rules indexed; got:\n{out}"
        )

    def test_no_rules_with_metadata_is_not_clean_bill(self, banner_writer):
        """Version with SpecMetadata present but zero LintRules -> Tier-1 wins.

        An empty rule set means no check was performed. Even if curate_status is
        'pending', an empty-index version cannot vouch for any code (WI-6 3-tier
        design: empty-rules check wins before pending-check).

        _BANNER_V2 is seeded here with curate_status='pending' but no LintRules.
        """
        import src.mcp.server as spec_tools

        # Seed SpecMetadata with curate_status pending, but NO LintRule.
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V2, curate_status="pending",
        )
        out = spec_tools._lint_check("x = 1", _BANNER_V2, language="python")
        assert "not a clean bill" in out.lower(), (
            "Tier-1 (empty rules) must win over Tier-2 (pending) when rules == []:\n"
            f"{out}"
        )


# ---------------------------------------------------------------------------
# Tier-2: pending banner (rules present but curation incomplete)
# ---------------------------------------------------------------------------

class TestLintCheckPendingDataShowsCurationBanner:
    def test_lint_check_pending_data_shows_curation_banner(
        self, banner_writer, neo4j_driver,
    ):
        """_lint_check output includes curation banner when curate_status='pending' + rules present.

        Tier-2 disclosure: if rules exist but curation is still pending, a soft
        'pending curation' banner is prepended. This is distinct from Tier-1 (empty
        index) which emits a hard warning.

        Fix for WI-6 behavior change: the old test seeded zero LintRules so Tier-1
        (empty-index hard warning) fired instead of Tier-2 (pending banner). The fix
        seeds >=1 LintRule for _BANNER_V so Tier-2 is actually exercised.
        """
        import sys
        sys.path.insert(0, ".")
        import src.mcp.server as spec_tools

        # Must seed >=1 LintRule for _BANNER_V so that rules != [] and the pending
        # soft-banner path (Tier-2) fires. Without this, empty-rules Tier-1 wins.
        banner_writer.write_lint_rules([
            LintRuleInfo(
                rule_id="W9999",
                odoo_version=_BANNER_V,
                kind="pylint-odoo",
                message="Placeholder lint rule for pending-banner test.",
                severity="warning",
            )
        ])

        # Seed SpecMetadata with curate_status pending.
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V, curate_status="pending",
        )

        out = spec_tools._lint_check("x = 1", _BANNER_V, language="python")
        assert "pending curation" in out.lower() or "pending" in out.lower(), (
            f"Expected 'pending' banner in output, got:\n{out}"
        )
        # Tier-1 hard warning must NOT appear when rules are present.
        assert "not a clean bill" not in out.lower(), (
            f"Hard 'NOT a clean bill' must not appear when rules are present:\n{out}"
        )

    def test_lint_check_no_banner_when_curate_status_done(
        self, banner_writer, neo4j_driver,
    ):
        """No curation banner when curate_status = 'done'.

        _BANNER_V2 was seeded with curate_status='pending' + no rules earlier, so
        it will hit Tier-1. This test uses _BANNER_V2 which has pending+no-rules,
        but what matters is checking that a 'done' version has no pending banner.
        We re-use _BANNER_V2 seeded as 'done' after the earlier fixture runs.
        """
        import src.mcp.server as spec_tools

        # Overwrite _BANNER_V2 as 'done' (no-op if already done).
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V2, curate_status="done",
        )
        out = spec_tools._lint_check("x = 1", _BANNER_V2, language="python")
        assert "pending curation" not in out.lower()


# ---------------------------------------------------------------------------
# Tier-3: complete + rules -> no disclosure banner (normal output)
# ---------------------------------------------------------------------------

class TestLintCheckCompleteVersionNoBanner:
    """When curate_status='complete' and rules exist, output has no disclosure banners."""

    def test_complete_version_no_pending_banner(self, banner_writer):
        """A fully curated version must not emit any pending-curation or NOT-clean-bill banner.

        The V0.5 hybrid-matcher banner (_LINT_V0_BANNER) is always present; this test
        only asserts that the disclosure banners are absent.
        """
        import src.mcp.server as spec_tools

        banner_writer.write_lint_rules([
            LintRuleInfo(
                rule_id="W9998",
                odoo_version=_BANNER_V3,
                kind="pylint-odoo",
                message="Placeholder lint rule for complete-version test.",
                severity="warning",
            )
        ])
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V3, curate_status="complete",
        )

        out = spec_tools._lint_check("x = 1", _BANNER_V3, language="python")
        assert "not a clean bill" not in out.lower(), (
            f"Complete+rules must not emit hard warning:\n{out}"
        )
        assert "pending curation" not in out.lower(), (
            f"Complete+rules must not emit pending curation banner:\n{out}"
        )


# ---------------------------------------------------------------------------
# CLI help pending banner (unchanged from original test)
# ---------------------------------------------------------------------------

class TestCliHelpPendingDataShowsCurationBanner:
    def test_cli_help_pending_data_shows_curation_banner(
        self, banner_writer, neo4j_driver,
    ):
        """_cli_help output includes curation banner when CLI curate_status = 'pending'."""
        import src.mcp.server as spec_tools

        banner_writer.write_spec_metadata(
            kind="cli", odoo_version=_BANNER_V, curate_status="pending",
        )
        # No CLI commands indexed for _BANNER_V - tests banner alone.
        out = spec_tools._cli_help("server", flag=None, odoo_version=_BANNER_V)
        assert "pending" in out.lower(), (
            f"Expected 'pending' in CLI help output, got:\n{out}"
        )
