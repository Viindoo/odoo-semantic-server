# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_spec_curation_banner.py
"""Pure unit + integration tests for _curate_status pending banner (PR#11 WI-F7).

Tests:
1. Unit: _lint_check output has curation banner when SpecMetadata.curate_status = 'pending'
2. Unit: _cli_help output has curation banner when curate_status = 'pending'
3. Unit: banner absent when curate_status = 'done'
"""
import os

import pytest

from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_BANNER_V = "95.0"
_BANNER_V2 = "96.0"


@pytest.fixture(scope="module")
def banner_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    yield writer
    with neo4j_driver.session() as session:
        for v in (_BANNER_V, _BANNER_V2):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    writer.close()


class TestLintCheckPendingDataShowsCurationBanner:
    def test_lint_check_pending_data_shows_curation_banner(
        self, banner_writer, neo4j_driver,
    ):
        """_lint_check output includes curation banner when curate_status = 'pending'."""
        import sys
        sys.path.insert(0, ".")
        import src.mcp.server as spec_tools

        # Seed SpecMetadata with curate_status pending
        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V, curate_status="pending",
        )

        out = spec_tools._lint_check("x = 1", _BANNER_V, language="python")
        assert "pending curation" in out.lower() or "pending" in out.lower(), (
            f"Expected 'pending' banner in output, got:\n{out}"
        )

    def test_lint_check_no_banner_when_curate_status_done(
        self, banner_writer, neo4j_driver,
    ):
        """No curation banner when curate_status = 'done'."""
        import src.mcp.server as spec_tools

        banner_writer.write_spec_metadata(
            kind="lint", odoo_version=_BANNER_V2, curate_status="done",
        )
        out = spec_tools._lint_check("x = 1", _BANNER_V2, language="python")
        assert "pending curation" not in out.lower()


class TestCliHelpPendingDataShowsCurationBanner:
    def test_cli_help_pending_data_shows_curation_banner(
        self, banner_writer, neo4j_driver,
    ):
        """_cli_help output includes curation banner when CLI curate_status = 'pending'."""
        import src.mcp.server as spec_tools

        banner_writer.write_spec_metadata(
            kind="cli", odoo_version=_BANNER_V, curate_status="pending",
        )
        # No CLI commands indexed for _BANNER_V — tests banner alone
        out = spec_tools._cli_help("server", flag=None, odoo_version=_BANNER_V)
        assert "pending" in out.lower(), (
            f"Expected 'pending' in CLI help output, got:\n{out}"
        )
