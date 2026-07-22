# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring regression guard for the #364 spec prune-on-full-write.

``pipeline.index_core()`` must invoke the three version-scoped spec prunes
(``prune_lint_rules`` / ``prune_cli_commands`` / ``prune_cli_flags``) EXACTLY
ONCE each, with the run's ``odoo_version``, right after the matching
``write_*`` — closing the orphan-on-remove gap (e.g. #364 dropped ``W8140``
from v14-v19) end to end.

Mirrors the R1 PatternExample prune wiring tests
(``tests/test_writer_patterns_prune.py``): it drives the REAL ``index_core``
(parsers monkeypatched to controlled outputs) and asserts the OBSERVABLE
outcome — a stale node written before the run is DETACH DELETEd while kept
nodes survive — not merely that a method name appears in the source. Because
``write_*`` is MERGE-only (never deletes), the disappearance of the pre-seeded
stale node can ONLY be caused by the prune actually running.

Requires Neo4j. Mark: pytest.mark.neo4j.
"""
import os

import pytest

from src.indexer.models import CLICommandInfo, CLIFlagInfo, LintRuleInfo
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Dedicated test version — grep-unique across the suite's version literals.
WIRE_VERSION = "97.5"


@pytest.fixture
def wire_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    def _wipe():
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=WIRE_VERSION,
            )

    _wipe()
    yield writer
    _wipe()
    writer.close()


def test_index_core_invokes_the_three_spec_prunes_once_each(
    wire_writer, neo4j_driver, tmp_path, monkeypatch,
):
    """index_core fires prune_lint_rules/prune_cli_commands/prune_cli_flags once
    each with the run's version, and the stale pre-seeded nodes are removed."""
    import src.indexer.framework_bases as fb
    import src.indexer.parser_cli as pcli
    import src.indexer.parser_lint_rules as plint
    import src.indexer.parser_odoo_core as pcore
    import src.indexer.parser_tools_symbols as ptools
    import src.indexer.pipeline as pipeline

    # The FULL sets this run "parses" for WIRE_VERSION (none of them stale).
    kept_rules = [
        LintRuleInfo("E9001", WIRE_VERSION, "pylint-odoo"),
        LintRuleInfo("E9002", WIRE_VERSION, "pylint-odoo"),
    ]
    kept_cmds = [
        CLICommandInfo("server", WIRE_VERSION),
        CLICommandInfo("shell", WIRE_VERSION),
    ]
    kept_flags = [
        CLIFlagInfo("--http-port", "server", WIRE_VERSION),
        CLIFlagInfo("--data-dir", "server", WIRE_VERSION),
    ]

    # Stub every parser index_core calls so source_root is never actually read.
    monkeypatch.setattr(pcore, "parse_odoo_core", lambda *a, **k: [])
    monkeypatch.setattr(pcore, "seed_framework_test_helpers", lambda *a, **k: [])
    monkeypatch.setattr(ptools, "load_tools_symbols", lambda *a, **k: [])
    monkeypatch.setattr(fb, "framework_bases", lambda *a, **k: [])
    monkeypatch.setattr(
        plint, "parse_lint_rules_for_version", lambda *a, **k: list(kept_rules),
    )
    monkeypatch.setattr(pcli, "parse_cli_commands", lambda *a, **k: list(kept_cmds))
    monkeypatch.setattr(pcli, "parse_cli_flags", lambda *a, **k: list(kept_flags))
    monkeypatch.setattr(pipeline, "_read_spec_curate_status", lambda *a, **k: "complete")
    monkeypatch.setattr(pipeline, "_find_previous_indexed_version", lambda *a, **k: None)

    # Pre-seed STALE nodes at WIRE_VERSION that the parsed sets DO NOT contain.
    wire_writer.write_lint_rules([LintRuleInfo("W8140", WIRE_VERSION, "pylint-odoo")])
    wire_writer.write_cli_commands([CLICommandInfo("obsolete-cmd", WIRE_VERSION)])
    wire_writer.write_cli_flags(
        [CLIFlagInfo("--longpolling-port", "server", WIRE_VERSION)]
    )

    # Spy on the three prunes: record the version each is called with, delegating
    # to the real implementation so the actual delete still happens.
    calls: dict[str, list[str]] = {"lint": [], "cmd": [], "flag": []}
    for meth, bucket in (
        ("prune_lint_rules", "lint"),
        ("prune_cli_commands", "cmd"),
        ("prune_cli_flags", "flag"),
    ):
        orig = getattr(wire_writer, meth)

        def _spy(version, live, _orig=orig, _bucket=bucket):
            calls[_bucket].append(version)
            return _orig(version, live)

        monkeypatch.setattr(wire_writer, meth, _spy)

    pipeline.index_core(
        source_root=str(tmp_path),
        odoo_version=WIRE_VERSION,
        writer=wire_writer,
        static_data_dir=str(tmp_path),
    )

    # 1) Each prune fired EXACTLY once, with the run's odoo_version.
    assert calls["lint"] == [WIRE_VERSION], f"prune_lint_rules calls: {calls['lint']}"
    assert calls["cmd"] == [WIRE_VERSION], f"prune_cli_commands calls: {calls['cmd']}"
    assert calls["flag"] == [WIRE_VERSION], f"prune_cli_flags calls: {calls['flag']}"

    # 2) Observable outcome: stale nodes gone, kept nodes present.
    with neo4j_driver.session() as session:
        rule_ids = set(session.run(
            "MATCH (l:LintRule {odoo_version:$v}) RETURN collect(l.rule_id) AS x",
            v=WIRE_VERSION,
        ).single()["x"])
        cmd_names = set(session.run(
            "MATCH (c:CLICommand {odoo_version:$v}) RETURN collect(c.name) AS x",
            v=WIRE_VERSION,
        ).single()["x"])
        flag_keys = set(session.run(
            "MATCH (f:CLIFlag {odoo_version:$v}) "
            "RETURN collect(f.flag_name + '|' + coalesce(f.command_name,'')) AS x",
            v=WIRE_VERSION,
        ).single()["x"])

    assert "W8140" not in rule_ids and {"E9001", "E9002"} <= rule_ids, (
        f"LintRule prune not wired into index_core; ids={sorted(rule_ids)}"
    )
    assert "obsolete-cmd" not in cmd_names and {"server", "shell"} <= cmd_names, (
        f"CLICommand prune not wired into index_core; names={sorted(cmd_names)}"
    )
    assert "--longpolling-port|server" not in flag_keys, (
        f"CLIFlag prune not wired into index_core; keys={sorted(flag_keys)}"
    )
    assert {"--http-port|server", "--data-dir|server"} <= flag_keys, (
        f"kept CLIFlags missing after index_core; keys={sorted(flag_keys)}"
    )
