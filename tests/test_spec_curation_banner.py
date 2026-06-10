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
_BANNER_V5 = "94.0"   # rules present + NO SpecMetadata -> matcher fires + "unknown" banner


@pytest.fixture(scope="module")
def banner_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2, _BANNER_V3, _BANNER_V4, _BANNER_V5):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    yield writer
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2, _BANNER_V3, _BANNER_V4, _BANNER_V5):
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
# Rules present + NO SpecMetadata (curate_status=None): matcher still fires +
# a soft "curation status unknown" banner. Regression for PR #275 HIGH #1 - the
# crash window between write_lint_rules and write_spec_metadata (separate Neo4j
# sessions), or any version indexed before write_spec_metadata existed.
# ---------------------------------------------------------------------------

class TestLintCheckRulesPresentNoMetadataStillMatches:
    """Rules indexed but SpecMetadata absent must NOT suppress real findings."""

    def test_rules_present_no_metadata_runs_matcher_and_warns_unknown(
        self, banner_writer,
    ):
        """rules present + curate_status=None -> matcher fires + 'unknown' banner.

        PR #275 HIGH #1: the old gate ``if not rules or curate_status is None``
        hard-returned the empty-index "no rules indexed ... NOT a clean bill"
        message even when rules existed - suppressing all real violations behind
        a doubly-false message. The fix splits the gate: ``not rules`` keeps the
        hard return; ``rules`` + ``curate_status is None`` runs the matcher and
        prepends a distinct soft "curation status unknown" banner.

        This regression test was entirely missing (existing Tier-1 tests cover
        only the zero-rules case). _BANNER_V5 is seeded with a real W8140 rule
        (carrying its code_pattern) but NO SpecMetadata, and the input code is a
        SQL-injection snippet that W8140's pattern fires on.
        """
        import src.mcp.server as spec_tools

        # Seed the real W8140 rule WITH its production code_pattern, so the
        # pattern-first matcher can deterministically fire. No SpecMetadata.
        banner_writer.write_lint_rules([
            LintRuleInfo(
                rule_id="W8140",
                odoo_version=_BANNER_V5,
                kind="pylint-odoo",
                message="SQL injection risk: `cr.execute` called with string interpolation.",
                severity="error",
                code_pattern=(
                    r"\.execute\s*\([^)]*?[\"']\s*%\s"
                    r"|\bexecute\s*\([^)]*?\.format\s*\("
                    r"|\.execute\s*\(\s*f[\"']"
                ),
            )
        ])
        # Deliberately do NOT write_spec_metadata for _BANNER_V5.

        code = "self.env.cr.execute('SELECT id FROM p WHERE n=%s' % self.name)"
        out = spec_tools._lint_check(code, _BANNER_V5, language="python")

        # 1. The matcher must have fired - the real finding is NOT suppressed.
        assert "W8140" in out, (
            "W8140 must fire when rules are indexed even with no SpecMetadata; "
            f"the finding must not be suppressed.\n{out}"
        )
        # 2. The soft "unknown" banner must be present...
        assert "curation status unknown" in out.lower(), (
            f"Expected soft 'curation status unknown' banner; got:\n{out}"
        )
        # 3. ...and it must NOT be the false empty-index message.
        assert "not a clean bill" not in out.lower(), (
            "Tier-1 'NOT a clean bill' must NOT appear when rules are present:\n"
            f"{out}"
        )
        # 4. ...nor the pending banner (distinct state).
        assert "pending curation" not in out.lower(), (
            f"'pending curation' banner must not appear for unknown state:\n{out}"
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

    def test_lint_check_no_banner_when_curate_status_done_and_rules_present(
        self, banner_writer, neo4j_driver,
    ):
        """A non-'pending', non-empty version emits NEITHER disclosure banner.

        Business rule: only ``curate_status == 'pending'`` (Tier-2) emits the soft
        "pending curation" banner, and only an empty rule set / missing status
        (Tier-1) emits the hard "NOT a clean bill" warning. Any OTHER status
        (e.g. the legacy 'done' alias) WITH rules present must fall through to
        normal output (Tier-3) — no disclosure banner at all.

        This test seeds >=1 LintRule for the version so it does NOT collapse into
        the vacuous Tier-1 path (an earlier version of this test asserted on a
        no-rules version and therefore passed trivially regardless of status).
        It now fails if 'done' were ever routed to Tier-2 (would add the pending
        banner) or to Tier-1 (would add the hard warning despite rules existing).
        """
        import src.mcp.server as spec_tools

        # banner_writer is module-scoped, so reset _BANNER_V2 first (a sibling
        # test seeds it as pending+no-rules) to keep this test order-independent.
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_BANNER_V2,
            )
        banner_writer.write_lint_rules([
            LintRuleInfo(
                rule_id="W9997",
                odoo_version=_BANNER_V2,
                kind="pylint-odoo",
                message="Placeholder lint rule for done-status test.",
                severity="warning",
            )
        ])
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V2, curate_status="done",
        )
        out = spec_tools._lint_check("x = 1", _BANNER_V2, language="python")
        # Tier-2 soft banner absent.
        assert "pending curation" not in out.lower()
        # Tier-1 hard warning absent (rules ARE present for this version).
        assert "not a clean bill" not in out.lower()


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
