# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two lifecycle leaks closed in finalfix round 4 (G1, G2).

G1 - ``--no-embed``: an index run without an embedder still runs the entity
prune on the graph; before G1 it left the removed entity's embedding rows
behind (the chunk keys were only built when an embedder existed), so
``find_examples`` kept returning a field the module no longer has. Rule: "a
--no-embed run that removes field X deletes X's rows of the owning profile,
keeps the (older) rows of the live fields, and computes no embedding".

G2 - co-owner exclusion: repos A and B both ship M; A flips M to
``installable: False``. A no longer ships M, but the node kept A's profile (and
A's rows): the node stayed co-owned, which the read-side choke answers
fail-closed, so even tenant B - who still ships M - saw it as not indexed. Rule: "after
A's run M's profile is exactly B's, A's rows of M are gone, B's kept, B
re-parses M next run, later runs change nothing; the audit predicts it (a
``would_drop_owner`` finding)". A module whose ONLY repo excludes it keeps
leaving through the orphan sweep
(``test_module_lifecycle_pipeline.py::test_installable_false_excludes_modules_without_tripping_the_gate``
- unchanged, in the same suite run).

Probe (coordinator round 3): A and B in ONE profile, A had test classes for M.
The node's profile does not change there (the survivor has the same profile),
so A's TestClass / TestMethod copies must still leave.

Real driver: every test runs the real ``index_profile`` (and CLI audit) over temp
git repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from tests._lifecycle_repo import (
    GitRepo,
    V,
    child_profiles,
    embeddings,
    ledger,
    module_node,
    register,
    run,
    write_module,
)
from tests.test_mcp_module_lifecycle_read import (  # noqa: F401 - fixtures
    _check,
    _lines,
    as_tenant,
    graph,
    hub,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

M = "viin_meeting_room"
PA, PB = "pa_99", "pb_99"


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


def _rows(pg_conn, module: str, profile: str, entity_suffix: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND profile_name = %s AND entity_name LIKE %s",
            (V, module, profile, f"%{entity_suffix}"),
        )
        return cur.fetchone()[0]


def _field(driver, name: str, module: str = M) -> int:
    with driver.session() as s:
        return s.run(
            "MATCH (f:Field {name: $n, module: $m, odoo_version: $v}) RETURN count(f) AS n",
            n=name, m=module, v=V,
        ).single()["n"]


def _audit(monkeypatch, capsys, *argv: str) -> dict:
    from src.indexer.__main__ import main
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    main(["lifecycle-audit", *argv, "--json"])
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# G1 - --no-embed still removes a pruned entity's rows
# ---------------------------------------------------------------------------

def test_no_embed_run_deletes_the_removed_fields_rows_and_keeps_live_rows(
    pg, neo4j_driver, tmp_path,
):
    """viin_meeting_room is indexed with an embedder (rows for label,
    room_note and action_touch). A commit removes room_note; the next run is
    --no-embed. room_note leaves the graph AND the profile's embeddings; the
    rows of label and action_touch - written by the earlier run, not re-embedded
    now - stay."""
    repo = GitRepo(tmp_path, "viindoo_addons")
    write_module(repo, M, extra_field="room_note = fields.Text()")
    repo.commit("add viin_meeting_room")
    register(PA, repo)
    run(pg, PA)
    assert _rows(pg, M, PA, ".room_note") == 1, "precondition: room_note embedded"
    label_before = _rows(pg, M, PA, ".label")
    method_before = _rows(pg, M, PA, ".action_touch")
    assert label_before == 1 and method_before == 1

    write_module(repo, M)
    repo.commit(f"[REM] {M}: drop room_note")
    summary = run(pg, PA, embedder=None)

    assert summary["modules"] >= 1, summary
    assert _field(neo4j_driver, "room_note") == 0, "precondition: the graph prune ran"
    assert _rows(pg, M, PA, ".room_note") == 0, "the pruned field kept its embedding row"
    assert _rows(pg, M, PA, ".label") == label_before, "a live field lost its older row"
    assert _rows(pg, M, PA, ".action_touch") == method_before


# ---------------------------------------------------------------------------
# G2 - a co-owner that excludes the module leaves its node
# ---------------------------------------------------------------------------

def _tenant(pg_conn, name: str, profile: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))
        tid = cur.fetchone()[0]
        cur.execute("UPDATE profiles SET tenant_id = %s WHERE name = %s", (tid, profile))
    return tid


def _indexed(out: str) -> str:
    lines = [ln for ln in _lines(out) if ln.startswith("├─ Indexed:")]
    assert lines, out
    return lines[0].split(":", 1)[1].strip()


@pytest.mark.usefixtures("hub")
def test_repo_that_excludes_a_module_another_repo_ships_leaves_the_node(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """Tenant A (repo addons_a, profile pa_99) and tenant B (addons_b, pb_99)
    both ship viin_meeting_room. A flips it to installable False. The audit
    predicts the owner drop (a would_drop_owner finding naming both sides). A's
    next run leaves the node owned by pb_99 alone - Module and every child -
    removes pa_99's rows of the module and keeps pb_99's, marks B to re-write
    the module. The co-owned node was hidden from both single-tenant callers
    (fail-closed, ADR-0034); now tenant B - the one that still ships it - sees
    it indexed and tenant A does not.
    B's next run re-parses it, and the runs after change nothing."""
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        write_module(repo, M)
        write_module(repo, f"{repo.path.name}_own")
        repo.commit("ship viin_meeting_room")
    (rid_a,) = register(PA, a)
    (rid_b,) = register(PB, b)
    tenant_a = _tenant(pg, "tenant_a", PA)
    tenant_b = _tenant(pg, "tenant_b", PB)
    run(pg, PA)
    run(pg, PB)
    assert PA in (module_node(neo4j_driver, M) or {}).get("profile", []), (
        "precondition: the node is co-owned"
    )
    assert embeddings(pg, M, PA) > 0 and embeddings(pg, M, PB) > 0
    # A node two tenants' profiles co-own is fail-closed (ADR-0034): neither
    # single-tenant caller sees it while both own it.
    for tenant in (tenant_a, tenant_b):
        with as_tenant(tenant):
            assert _indexed(_check(M)) == "No", "precondition: the co-owned node is fail-closed"

    write_module(a, M, installable=False)
    a.commit(f"[MIG] {M}: not ported, installable False")

    report = _audit(monkeypatch, capsys, "--all")
    [ver] = [v for v in report["versions"] if v["odoo_version"] == V]
    predicted = ver.get("would_drop_excluded_owner") or {}
    assert list(predicted) == [M], ver
    assert any("addons_a" in x for x in predicted[M]["excluded_by"]), predicted
    assert any("addons_b" in x for x in predicted[M]["kept_by"]), predicted
    assert report["findings"]["would_drop_owner"] >= 1

    run(pg, PA)

    assert ledger(pg, rid_a, M)["state"] == "excluded"
    node = module_node(neo4j_driver, M)
    assert node is not None and node["profile"] == [PB], node
    assert child_profiles(neo4j_driver, M) == {(PB,)}, "a child still carries pa_99"
    assert embeddings(pg, M, PA) == 0, "the excluding profile kept its rows of the module"
    assert embeddings(pg, M, PB) > 0, "the survivor's rows must stay"
    assert ledger(pg, rid_b, M)["needs_rewrite"] is True
    with as_tenant(tenant_a):
        assert _indexed(_check(M)) == "No", "tenant A still sees a module it no longer ships"
    with as_tenant(tenant_b):
        assert _indexed(_check(M)) == "Yes"

    assert run(pg, PB)["modules"] >= 1, "B re-writes the module"
    assert ledger(pg, rid_b, M)["needs_rewrite"] is False
    for profile in (PA, PB):
        assert run(pg, profile)["modules"] == 0, profile
    assert module_node(neo4j_driver, M)["profile"] == [PB]
    assert child_profiles(neo4j_driver, M) == {(PB,)}


# ---------------------------------------------------------------------------
# Probe - the excluding repo shares its profile with the survivor
# ---------------------------------------------------------------------------

def _with_tests(repo: GitRepo, class_name: str) -> None:
    tests = repo.path / M / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("from . import test_room\n")
    (tests / "test_room.py").write_text(textwrap.dedent(f"""\
        from odoo.tests.common import TransactionCase


        class {class_name}(TransactionCase):
            def test_touch(self):
                self.assertTrue(self.env["viin.meeting.room.record"].action_touch())
        """))


def _test_nodes(driver, repo_basename: str) -> dict[str, int]:
    with driver.session() as s:
        row = s.run(
            """
            OPTIONAL MATCH (c:TestClass {module: $m, odoo_version: $v}) WHERE c.repo = $r
            WITH count(c) AS classes
            OPTIONAL MATCH (t:TestMethod {module: $m, odoo_version: $v}) WHERE t.repo = $r
            RETURN classes, count(t) AS methods
            """,
            m=M, v=V, r=repo_basename,
        ).single()
    return {"classes": row["classes"], "methods": row["methods"]}


def test_excluding_repo_in_the_survivors_profile_loses_its_test_copies(
    pg, neo4j_driver, tmp_path,
):
    """addons_a and addons_b are both in profile shared_99 and ship
    viin_meeting_room, each with its own test class. addons_a flips the module
    to installable False: the node keeps the profile (the survivor has it), but
    addons_a no longer ships the module, so its TestClass / TestMethod copies
    must leave while addons_b's stay."""
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo, cls in ((a, "TestRoomA"), (b, "TestRoomB")):
        write_module(repo, M)
        _with_tests(repo, cls)
        repo.commit("ship viin_meeting_room with tests")
    rid_a, _rid_b = register("shared_99", a, b)
    run(pg, "shared_99")
    assert _test_nodes(neo4j_driver, "addons_a") == {"classes": 1, "methods": 1}, (
        "precondition: addons_a's test copy is indexed"
    )
    assert _test_nodes(neo4j_driver, "addons_b") == {"classes": 1, "methods": 1}

    write_module(a, M, installable=False)
    a.commit(f"[MIG] {M}: installable False")
    run(pg, "shared_99")
    run(pg, "shared_99")

    assert ledger(pg, rid_a, M)["state"] == "excluded"
    assert module_node(neo4j_driver, M)["profile"] == ["shared_99"]
    assert _test_nodes(neo4j_driver, "addons_b") == {"classes": 1, "methods": 1}
    assert _test_nodes(neo4j_driver, "addons_a") == {"classes": 0, "methods": 0}, (
        "the excluding repo's test copies stayed"
    )
