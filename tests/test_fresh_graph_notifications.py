# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indexing a graph that lacks optional keys / relationship types makes Neo4j
emit no Unknown{PropertyKey,RelationshipType}Warning (F50).

Rules:

* reading the lifecycle records (degraded parse, held prune, deferred prune) on
  a graph where no module ever had one returns ``[]`` and emits no warning; a
  record written and read back is returned (the read still reads);
* an index run (and the dry-run ``lifecycle-audit``) over repos without OWL
  components, test helpers or manifest dependencies emits no
  UnknownPropertyKeyWarning / UnknownRelationshipTypeWarning notification, and
  no warning naming the never-written ``_SeedMeta`` sentinel label (round 3).

Real case (F50): every healthy production graph has never had a degraded parse
or a held prune, so each index run (per repo) and each weekly audit logged a
WARNING per unknown key per call - 14 for one call of each record read on a
fresh graph, and dozens per run from the OWL / test-surface / DEPENDS_ON
post-pass reads (``extends``, ``is_helper``, ``base_classes_ordered``,
``EXTENDS``, ``INHERITS_TEST``, ``DEPENDS_ON`` ...). Operators learn to ignore
a log that always warns, which hides the warning that matters.

"Fresh graph" must be literal: Neo4j keeps a property-key / relationship-type
token once any node ever had it, even after the node is deleted, so the shared
session container (where earlier tests wrote degraded records, OWL edges...)
cannot show the warning at all. Each test therefore starts its own empty Neo4j
container, and a positive control proves the capture sees an unknown-key
warning before any "zero warnings" verdict is trusted.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid

import pytest

from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import _NEO4J_IMAGE, TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
_UNKNOWN_CODES = ("UnknownPropertyKeyWarning", "UnknownRelationshipTypeWarning")


@pytest.fixture
def fresh_neo4j(monkeypatch):
    """A brand-new, empty Neo4j (own container); NEO4J_* env points at it."""
    try:
        from testcontainers.core.wait_strategies import LogMessageWaitStrategy
        from testcontainers.neo4j import Neo4jContainer
    except ImportError as exc:  # pragma: no cover - dev dependency
        pytest.skip(f"testcontainers unavailable: {exc}")

    class _Neo4jContainer(Neo4jContainer):
        def _connect(self) -> None:  # the conftest override: no deprecated wait
            with self.get_driver() as driver:
                driver.verify_connectivity()

    container = _Neo4jContainer(_NEO4J_IMAGE).waiting_for(
        LogMessageWaitStrategy("Remote interface available at")
    )
    try:
        container.start()
    except Exception as exc:  # noqa: BLE001 - no Docker: nothing fresh to test on
        pytest.skip(f"cannot start a dedicated Neo4j container: {exc}")
    try:
        url = container.get_connection_url()
        monkeypatch.setenv("NEO4J_URI", url)
        monkeypatch.setenv("NEO4J_USER", "neo4j")
        monkeypatch.setenv("NEO4J_PASSWORD", "password")
        yield url
    finally:
        container.stop()


_MISSING = re.compile(r"the missing (?:property|relationship type)? ?name is: ([^)]+)\)")


def _unknown(caplog) -> list[str]:
    """``<code>: <missing name>`` of every captured unknown-key / -type warning."""
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if r.name != "neo4j.notifications":
            continue
        for code in _UNKNOWN_CODES:
            if code in msg:
                missing = _MISSING.search(msg)
                out.append(f"{code}: {missing.group(1) if missing else msg[:300]}")
    return out


def _seed_meta_warnings(caplog) -> list[str]:
    """Captured notifications naming the pattern-reseed sentinel label."""
    return [
        r.getMessage()[:300] for r in caplog.records
        if r.name == "neo4j.notifications" and "_SeedMeta" in r.getMessage()
    ]


def _assert_capture_sees_unknown_keys(writer: Neo4jWriter, caplog) -> None:
    """Positive control: a read of a key no node ever had IS captured."""
    key = f"never_written_{uuid.uuid4().hex[:8]}"
    caplog.clear()
    with writer.driver.session() as s:
        s.run(f"MATCH (m:Module) RETURN m.{key} AS x").consume()
    assert any(key in msg for msg in _unknown(caplog)), (
        "the capture does not see Neo4j notifications - a zero count would prove nothing"
    )
    caplog.clear()


def test_lifecycle_record_reads_on_a_graph_that_never_had_a_record_do_not_warn(
    fresh_neo4j, caplog,
):
    """Degraded / held / deferred record reads on an indexed-looking graph with
    no record: ``[]``, zero unknown-key warnings. Then each record is written and
    read back (the read is not blind)."""
    writer = Neo4jWriter(fresh_neo4j, "neo4j", "password")
    try:
        writer.setup_indexes()
        with writer.driver.session() as s:
            s.run(
                "MERGE (m:Module {name: 'viin_ai_rag', odoo_version: $v}) "
                "SET m.profile = ['viindoo_99']", v=V,
            ).consume()
        caplog.set_level(logging.WARNING, logger="neo4j.notifications")
        _assert_capture_sees_unknown_keys(writer, caplog)

        assert writer.parse_degraded_modules(7) == []
        assert writer.prune_held_modules(7) == []
        assert writer.prune_deferred_modules(V) == []

        assert _unknown(caplog) == []

        writer.record_module_parse_degraded(
            V, "viin_ai_rag", repo_id=7, fingerprint="fp1",
            paths=["viin_ai_rag/models/x.py"], problems=["SyntaxError"],
        )
        writer.record_module_prune_held(
            V, "viin_ai_rag", repo_id=7, stale=26, total=32, rels_stale=0, rels_total=6,
        )
        writer.record_module_prune_deferred(V, "viin_ai_rag", repo_id=7, waits_for=[9])
        assert [r["fingerprint"] for r in writer.parse_degraded_modules(7)] == ["fp1"]
        assert [(r["stale"], r["total"]) for r in writer.prune_held_modules(7)] == [(26, 32)]
        assert [(r["name"], r["waits_for"]) for r in writer.prune_deferred_modules(V)] == [
            ("viin_ai_rag", [9])
        ]
    finally:
        writer.close()


@pytest.mark.postgres
def test_index_run_and_audit_on_a_fresh_graph_emit_no_unknown_key_or_type_warning(
    fresh_neo4j, clean_pg, tmp_path, caplog, monkeypatch, capsys,
):
    """A first deployment indexes a repo whose modules have no OWL component, no
    test helper and no manifest dependency (so no ``extends``, ``is_helper``,
    ``EXTENDS``, ``INHERITS_TEST`` or ``DEPENDS_ON`` ever exists), then a second
    run and the weekly ``lifecycle-audit``. None of it may emit an
    UnknownPropertyKey / UnknownRelationshipType warning, nor any warning naming
    the pattern-reseed sentinel label ``_SeedMeta`` (round 3: the never-seeded
    graph has no such label; the sentinel read and delete used to name it on
    every run). The runs use an embedder (FakeEmbedder), so the pattern reseed
    reads its sentinel exactly as in production."""
    from src.db.migrate import run_migrations
    from src.indexer.__main__ import main
    from tests import conftest
    from tests._lifecycle_repo import GitRepo, register, run, write_module

    run_migrations(clean_pg)
    repo = GitRepo(tmp_path, "viindoo_addons")
    write_module(repo, "viin_ai_rag")
    write_module(repo, "viin_ai")
    repo.commit("two modules, no dependencies, no OWL, no tests")
    register("viindoo_99", repo)
    writer = Neo4jWriter(fresh_neo4j, "neo4j", "password")
    caplog.set_level(logging.WARNING, logger="neo4j.notifications")
    try:
        _assert_capture_sees_unknown_keys(writer, caplog)
    finally:
        writer.close()

    first = run(clean_pg, "viindoo_99")
    second = run(clean_pg, "viindoo_99")
    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    code = main(["lifecycle-audit", "--profile", "viindoo_99", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert first["modules"] == 2, first
    assert second["modules"] == 0, second
    assert code == 0 and report["has_findings"] is False, report["findings"]
    assert os.environ["NEO4J_URI"] == fresh_neo4j, "the runs must have used the fresh graph"
    assert _unknown(caplog) == []
    assert _seed_meta_warnings(caplog) == [], "the never-seeded _SeedMeta label was named"
