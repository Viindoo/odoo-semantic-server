# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_flags_graph_parity.py
"""issue #117 bug#1 — served-graph vs spec_data JSON parity for CLI flags.

The v18 phantom ``--with-demo`` was a STALE-graph drift: the SSOT
``spec_data/cli_flags_18.0.json`` was corrected (2026-06-21 — ``--with-demo`` is
v19-only) but the Neo4j index was not rebuilt, so ``cli_help('18.0')`` kept
serving the phantom flag. ``test_spec_data_cli_flags_curated`` already guards the
JSON at rest; this test guards the OTHER half — the indexer -> graph -> cli_help
transfer: when a version's JSON SSOT is seeded, ``cli_help`` must serve EXACTLY
that flag set per command (no phantom add, no silent drop, no status drift).

The real ``cli_flags_18.0.json`` content is the SSOT under test; it is relabelled
onto a dedicated test version so the parity assertion never collides with (nor
depends on) any real v18 nodes in the shared test graph.
"""
import dataclasses
import os
import sys

import pytest

from src.indexer.parser_cli import (
    _load_static_cli_commands,
    _load_static_cli_flags,
)
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# SSOT content comes from this version's JSON ...
SOURCE_VERSION = "18.0"
# ... relabelled onto this disposable test-version key. Each issue #117 test owns
# a UNIQUE disposable version (68-72) so module-scoped seed/teardown never wipes a
# sibling fixture's nodes; not in _FORBIDDEN_VERSIONS (test_pattern_seed_no_test_pollution).
PARITY_VERSION = "68.0"


def _load_ssot():
    """Return (flags, commands) from the real v18 spec JSON, relabelled to PARITY_VERSION."""
    flags = [
        dataclasses.replace(f, odoo_version=PARITY_VERSION)
        for f in _load_static_cli_flags(SOURCE_VERSION, None)
    ]
    commands = [
        dataclasses.replace(c, odoo_version=PARITY_VERSION)
        for c in _load_static_cli_commands(SOURCE_VERSION, None)
    ]
    return flags, commands


@pytest.fixture(scope="module")
def seeded_cli_parity(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    flags, commands = _load_ssot()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PARITY_VERSION)
    writer.write_cli_commands(commands)
    writer.write_cli_flags(flags)
    writer.write_spec_metadata(
        kind="cli", odoo_version=PARITY_VERSION, curate_status="complete",
    )

    yield flags, commands

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PARITY_VERSION)
    writer.close()


@pytest.fixture
def cli_tool(seeded_cli_parity):
    """Fresh server generation bound to the test Neo4j (mirrors test_mcp_spec_tools)."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


def _ssot_flags_for_command(flags, command_name):
    return {f.flag_name for f in flags if f.command_name == command_name}


class TestCliFlagGraphParity:
    def test_no_phantom_with_demo_on_server(self, cli_tool, seeded_cli_parity):
        """The exact bug: cli_help must NOT serve --with-demo on the server command.

        --with-demo is v19-only; v18 only has --without-demo.
        """
        out = cli_tool._cli_help("server", flag=None, odoo_version=PARITY_VERSION)
        assert "--with-demo" not in out, (
            "cli_help served the phantom --with-demo flag (v19-only) for v18 — "
            "graph drifted from the corrected spec_data JSON."
        )
        assert "--without-demo" in out, (
            "cli_help dropped --without-demo, which IS present in the v18 spec JSON."
        )

    def test_server_flag_set_matches_ssot(self, cli_tool, seeded_cli_parity):
        """The served 'server' flag set must equal the spec_data JSON 'server' set.

        Parity in BOTH directions: any flag the resolver invents (graph cruft) or
        drops (incomplete index) fails here — the core drift guard.
        """
        flags, _ = seeded_cli_parity
        expected = _ssot_flags_for_command(flags, "server")
        # cli_help('server') lists every server flag (one per line, '<connector> --flag').
        out = cli_tool._cli_help("server", flag=None, odoo_version=PARITY_VERSION)
        served = {tok for tok in out.split() if tok.startswith("--")}
        missing = expected - served
        extra = served - expected
        assert not missing, f"cli_help dropped server flags present in SSOT JSON: {sorted(missing)}"
        assert not extra, f"cli_help served server flags absent from SSOT JSON: {sorted(extra)}"

    def test_without_demo_status_matches_ssot(self, cli_tool, seeded_cli_parity):
        """--without-demo detail (status) served must match the JSON SSOT entry."""
        flags, _ = seeded_cli_parity
        ssot = next(
            f for f in flags
            if f.flag_name == "--without-demo" and f.command_name == "server"
        )
        out = cli_tool._cli_help("server", flag="--without-demo", odoo_version=PARITY_VERSION)
        assert "--without-demo" in out
        # Default status is 'stable' when the JSON omits it.
        assert (ssot.status or "stable") in out.lower()
