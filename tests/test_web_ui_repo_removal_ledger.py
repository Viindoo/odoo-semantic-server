# SPDX-License-Identifier: AGPL-3.0-or-later
"""Removing a repo or a profile from the Web UI goes through the lifecycle ledger
(ADR-0056 B10, plan T10, review H3 / L7 / F1 / F21).

Business rules protected (real cases in brackets):

* T10 - removing a repo retires what ONLY that repo ships: the Module and its
  whole subtree (models, views, TestClass, JsTestSuite, Stylesheet,
  LintViolation ...) and that profile's embeddings - never another profile's
  embeddings, another name or another version [the Web UI "remove repo" button
  on the repo that shipped ``viin_ai_rag``].
* A module another repo still ships keeps its node; ownership becomes exactly
  the survivor's, the departed repo's test nodes go, the survivor is flagged
  ``needs_rewrite`` [``to_saas_base`` shipped by two repos mid-move].
* H3 - the ledger history survives the Postgres delete: every row of the removed
  repo ends ``retired`` / ``repo_removed`` (or still pending when undecidable)
  with ``repo_id`` NULL and its profile / URL / basename kept.
* F1 - every CE clone is named ``odoo`` (``<clone_dir>/<profile>/odoo``), so a
  removal by basename would retire the CE modules of EVERY profile at that
  version. Deleting profile A's ``odoo`` repo never touches profile B's.
* A node nobody attributed (no profile, no repo id: a dependency stub) is never
  deleted just because its ``repo`` property names the removed repo.
* F21 - when an index run / git operation / reconcile holds a lock the removal
  needs, the request answers 409 + Retry-After and NOTHING changes (Postgres,
  graph and embeddings identical).
* L7 - renaming a profile rewrites the ledger in the same transaction: history
  is readable under the new name only; a busy ledger lock means 409 and neither
  the name nor the ledger moved.
* L3 - a retirement re-links same-name INHERITS edges to the remaining
  definition, whether or not the version-wide sweep ran.
"""
from __future__ import annotations

import json
from datetime import timedelta

import httpx
import psycopg2
import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.web_ui.app import create_app
from tests import _ledger_seed as ls
from tests import _retirement_fixture as fx

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

V = ls.V
RAG, AI = fx.RETIRED, fx.SURVIVOR


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def stores(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + a clean graph at TEST_VERSION."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version IN (%s, %s)", (V, ls.OTHER_V))
    yield clean_pg, clean_neo4j
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (ls.OTHER_V,))


@pytest.fixture
def writer(stores):
    w = ls.open_writer()
    yield w
    w.close()


def _repo(tmp_path, writer, *, profile_id: int, profile: str, basename: str,
          modules: tuple[str, ...], head: str, synced: bool = True,
          ledger: tuple[str, ...] | None = None, viin_ai_tests: bool = False) -> int:
    """Register, index (graph) and observe (ledger) one repo."""
    repo_dir = ls.write_repo(tmp_path, profile, basename, modules=modules,
                             viin_ai_tests=viin_ai_tests)
    rid = ls.add_repo(profile_id, repo_dir)
    ls.index_graph(writer, repo_dir, profile=profile, repo_id=rid)
    names = modules if ledger is None else ledger
    if names:
        ls.observe(rid, profile, names, head=head, synced=synced)
    return rid


# ---------------------------------------------------------------------------
# T10 - repo delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_repo_retires_what_only_it_ships_and_keeps_the_rest(
    stores, writer, tmp_path,
):
    """Repo A (pa_99) ships viin_ai + viin_ai_rag, repo B (pb_99) ships viin_ai.
    Deleting A retires viin_ai_rag with its whole subtree and pa_99's embeddings,
    hands viin_ai to B alone, and keeps pb_99's, another tenant's and another
    version's embeddings."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                  modules=(AI, RAG), head="ha1")
    rid_b = _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="addons_b",
                  modules=(AI,), head="hb1")
    for module, profile in ((RAG, "pa_99"), (AI, "pa_99"), (AI, "pb_99"), (RAG, "tvtma_99")):
        ls.seed_embedding(module, profile)
    ls.seed_embedding(RAG, "pa_99", version=ls.OTHER_V)

    framework_before = ls.shared_framework_nodes(driver)
    rag_before = ls.subtree(driver, RAG)
    assert {"TestClass", "JsTestSuite", "Stylesheet", "LintViolation", "View"} <= set(
        rag_before), f"positive control: the fixture wrote the full surface: {rag_before}"
    assert ("pa_99", "pb_99") in ls.attributed_profiles(driver, AI), "positive control"
    emb_before = ls.embedding_groups(conn)

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["basename"] == "addons_a"

    # viin_ai_rag: nobody else ships it -> retired with every child.
    assert ls.module_node(driver, RAG) is None
    assert ls.subtree(driver, RAG) == {}
    assert fx.lint_violations_of(driver, RAG) == 0
    # viin_ai: B still ships it -> node kept, owned by B only.
    node = ls.module_node(driver, AI)
    assert node is not None
    assert node["profile"] == ["pb_99"]
    assert node["repos"] == ["addons_b"]
    assert node["repo"] == "addons_b"
    assert ls.attributed_profiles(driver, AI) == {("pb_99",)}
    # Shared framework / placeholder nodes are never part of a module's cascade.
    assert ls.shared_framework_nodes(driver) == framework_before

    # Embeddings: exactly the departing profile's rows of the two names go.
    assert ls.embedding_groups(conn) == {
        k: n for k, n in emb_before.items()
        if k not in {(RAG, V, "pa_99"), (AI, V, "pa_99")}
    }

    # The survivor rewrites the module it now owns alone.
    assert ls.presence_store().needs_rewrite_names(rid_b) == [AI]

    # H3: history survives the repo delete.
    rows = ls.ledger_rows(conn, "profile_name = %s", ("pa_99",))
    assert [r["name"] for r in rows] == sorted([AI, RAG])
    for r in rows:
        assert r["state"] == "retired", r
        assert r["retire_reason"] == "repo_removed", r
        assert r["repo_id"] is None, r
        assert r["repo_basename"] == "addons_a"
        assert r["repo_url"].endswith("/pa_99/addons_a.git")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repos WHERE id = %s", (rid_a,))
        assert cur.fetchone()[0] == 0

    summary = body["lifecycle"]["versions"][V]
    assert RAG in summary["retired"]
    assert AI in summary["owner_dropped"]
    assert body["neo4j_modules"] == 1
    assert body["embeddings"] == 2


@pytest.mark.asyncio
async def test_a_module_both_repos_ship_keeps_only_the_survivors_tests_and_profile(
    stores, writer, tmp_path,
):
    """to_saas_base mid-move: both repos ship viin_ai + viin_ai_rag. Removing A
    keeps both nodes; every node carries exactly pb_99; A's TestClass/TestMethod
    copies go, B's stay; only pa_99's embeddings go; B must rewrite both."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="saas_a",
                  modules=(AI, RAG), head="ha1", viin_ai_tests=True)
    rid_b = _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="saas_b",
                  modules=(AI, RAG), head="hb1", viin_ai_tests=True)
    for module in (AI, RAG):
        ls.seed_embedding(module, "pa_99")
        ls.seed_embedding(module, "pb_99")
    tests_before = {m: ls.tests_by_repo(driver, m) for m in (AI, RAG)}
    for m, t in tests_before.items():
        assert {repo for _lbl, repo in t} >= {"saas_a", "saas_b"}, (
            f"positive control: both repos wrote {m}'s tests: {t}")

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    for m in (AI, RAG):
        node = ls.module_node(driver, m)
        assert node is not None, f"{m} is still shipped by B"
        assert node["profile"] == ["pb_99"]
        assert node["repos"] == ["saas_b"]
        assert ls.attributed_profiles(driver, m) == {("pb_99",)}
        assert ls.tests_by_repo(driver, m) == {
            k: n for k, n in tests_before[m].items() if k[1] == "saas_b"
        }
    assert ls.embedding_groups(conn) == {(AI, V, "pb_99"): 1, (RAG, V, "pb_99"): 1}
    assert ls.presence_store().needs_rewrite_names(rid_b) == sorted([AI, RAG])
    assert {r["state"] for r in ls.ledger_rows(conn, "profile_name = 'pa_99'")} == {"retired"}


@pytest.mark.asyncio
async def test_deleting_one_profiles_odoo_clone_never_retires_another_profiles_ce_modules(
    stores, writer, tmp_path,
):
    """F1: both profiles clone CE to ``<dir>/<profile>/odoo``. Deleting pa_99's
    ``odoo`` repo leaves pb_99's modules, subtrees and embeddings in place."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="odoo",
                  modules=(AI, RAG), head="ce1")
    _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="odoo",
          modules=(AI, RAG), head="ce1")
    for module in (AI, RAG):
        ls.seed_embedding(module, "pb_99")
    subtree_before = {m: ls.subtree(driver, m) for m in (AI, RAG)}

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    for m in (AI, RAG):
        assert ls.module_node(driver, m) is not None, f"pb_99's CE module {m} was retired"
        assert ls.attributed_profiles(driver, m) == {("pb_99",)}
        assert ls.subtree(driver, m).keys() == subtree_before[m].keys()
    assert ls.embedding_groups(conn) == {(AI, V, "pb_99"): 1, (RAG, V, "pb_99"): 1}


@pytest.mark.asyncio
async def test_deleting_an_odoo_clone_keeps_ce_modules_of_a_profile_not_yet_synced(
    stores, writer, tmp_path,
):
    """F1 at rollout: pb_99's ``odoo`` clone was indexed before the ledger existed
    (no ledger rows, never synced). It may still ship every name, so deleting
    pa_99's ``odoo`` repo decides nothing: pb_99's nodes and embeddings stay and
    pa_99's rows wait (pending, repo_id NULL) for the next index run."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="odoo",
                  modules=(AI, RAG), head="ce1")
    _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="odoo",
          modules=(AI, RAG), head="ce1", ledger=())
    for module in (AI, RAG):
        ls.seed_embedding(module, "pb_99")

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    for m in (AI, RAG):
        node = ls.module_node(driver, m)
        assert node is not None, f"pb_99's unsynced CE module {m} was deleted"
        assert "pb_99" in node["profile"]
        assert ls.subtree(driver, m) != {}
    assert ls.embedding_groups(conn) == {(AI, V, "pb_99"): 1, (RAG, V, "pb_99"): 1}
    rows = ls.ledger_rows(conn, "profile_name = 'pa_99'")
    assert [r["name"] for r in rows] == sorted([AI, RAG])
    for r in rows:
        assert r["repo_id"] is None
        assert r["state"] != "retired"
        assert r["retire_pending"] is True
        assert r["retire_pending_reason"] == "repo_removed"
    assert set(resp.json()["lifecycle"]["versions"][V]["undecidable"]) == {AI, RAG}


@pytest.mark.asyncio
async def test_pre_ledger_residue_of_the_removed_repo_is_retired_but_a_stub_is_not(
    stores, writer, tmp_path,
):
    """A module A indexed before the ledger existed (graph node, no ledger row)
    goes with its subtree and pa_99's embeddings; a dependency stub whose
    ``repo`` names A but that nobody attributed survives."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                  modules=(AI, "ghost_a"), head="ha1", ledger=(AI,))
    with driver.session() as s:
        s.run("MERGE (m:Module {name: 'legacy_stub', odoo_version: $v}) SET m.repo = 'addons_a'",
              v=V)
    ls.seed_embedding("ghost_a", "pa_99")
    assert ls.subtree(driver, "ghost_a") != {}, "positive control"

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    assert ls.module_node(driver, "ghost_a") is None
    assert ls.subtree(driver, "ghost_a") == {}
    assert ls.module_node(driver, AI) is None
    assert ls.module_node(driver, "legacy_stub") is not None
    assert ls.embedding_groups(conn) == {}
    assert "ghost_a" in resp.json()["lifecycle"]["versions"][V]["residue_retired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pb_synced", [True, False], ids=["other_synced", "other_unsynced"])
async def test_residue_of_a_removed_odoo_clone_never_includes_another_profiles_modules(
    stores, writer, tmp_path, pb_synced,
):
    """Final review T3: both profiles clone CE to ``<dir>/<profile>/odoo`` and
    each has pre-ledger residue (a graph node the ledger never recorded):
    pa_99's ``ghost_a``, pb_99's ``pb_ghost``. Deleting pa_99's ``odoo`` repo
    retires and reports ghost_a only. pb_ghost is never retired, kept or even
    named in the answer (another tenant's module name), whether pb_99 is synced
    or not; its subtree and embeddings stay."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="odoo",
                  modules=(AI, "ghost_a"), head="ce1", ledger=(AI,))
    _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="odoo",
          modules=("pb_mod", "pb_ghost"), head="ce1", ledger=("pb_mod",), synced=pb_synced)
    ls.seed_embedding("pb_ghost", "pb_99")
    ls.seed_embedding("ghost_a", "pa_99")
    pb_ghost_before = ls.subtree(driver, "pb_ghost")
    assert pb_ghost_before, "positive control: pb_ghost has children"
    assert ls.module_node(driver, "pb_ghost")["repo"] == "odoo", "precondition: same basename"

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    summary = resp.json()["lifecycle"]["versions"][V]
    assert "ghost_a" in summary["residue_retired"], summary
    assert ls.module_node(driver, "ghost_a") is None
    assert "pb_ghost" not in resp.text, f"another profile's module was reported: {summary}"
    assert "pb_mod" not in resp.text, summary
    node = ls.module_node(driver, "pb_ghost")
    assert node is not None and node["profile"] == ["pb_99"], node
    assert ls.subtree(driver, "pb_ghost") == pb_ghost_before
    assert ls.module_node(driver, "pb_mod") is not None
    assert ls.embedding_groups(conn) == {("pb_ghost", V, "pb_99"): 1}


@pytest.mark.asyncio
async def test_removal_of_a_repo_leaves_a_name_another_unsynced_repo_may_ship(
    stores, writer, tmp_path,
):
    """Rule 3: a repo registered at the version but never synced may ship
    anything; the removed repo's names stay pending (repo_id NULL) and their
    nodes stay until the next index run decides them."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pc = ls.add_profile("pc_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                  modules=(AI,), head="ha1")
    # Cloned (a checkout exists) but never indexed: it may ship anything.
    ls.add_repo(pc, ls.write_repo(tmp_path, "pc_99", "never_indexed", modules=(AI,)))

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    assert ls.module_node(driver, AI) is not None
    [row] = ls.ledger_rows(conn, "profile_name = 'pa_99'")
    assert (row["state"], row["retire_pending"], row["repo_id"]) == ("present", True, None)


@pytest.mark.asyncio
async def test_a_removal_decides_only_the_removed_repos_names(stores, writer, tmp_path):
    # GUARD: pre-existing behaviour (the old basename delete never touched other
    # repos' names either); keeps reconcile_removed_repos restricted to *names*.
    """Another repo's own pending retirement (b_old, flagged by B's last scan but
    not yet reconciled) is the next index run's decision, not the delete's."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                  modules=(AI,), head="ha1")
    rid_b = _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="addons_b",
                  modules=("b_old",), head="hb1")
    ls.presence_store().mark_retire_pending(rid_b, ["b_old"], "absent")

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    assert ls.module_node(driver, AI) is None
    assert ls.module_node(driver, "b_old") is not None
    [row] = ls.ledger_rows(conn, "name = 'b_old'")
    assert (row["state"], row["retire_pending"], row["repo_id"]) == ("present", True, rid_b)


@pytest.mark.asyncio
async def test_without_neo4j_the_repo_is_removed_and_its_rows_wait_for_the_next_run(
    stores, writer, tmp_path, monkeypatch,
):
    """Neo4j not configured: the delete still removes the repo, keeps its history
    pending (repo_id NULL) for the next index run's reconcile and says so."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    rid_a = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                  modules=(AI,), head="ha1")
    monkeypatch.setattr("src.web_ui.routes.repos._get_neo4j_writer", lambda: None)

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")

    assert resp.status_code == 200, resp.text
    assert ls.module_node(driver, AI) is not None
    [row] = ls.ledger_rows(conn, "profile_name = 'pa_99'")
    assert (row["retire_pending"], row["retire_pending_reason"], row["repo_id"]) == (
        True, "repo_removed", None)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM repos WHERE id = %s", (rid_a,))
        assert cur.fetchone()[0] == 0
    lifecycle = resp.json().get("lifecycle") or {}
    assert lifecycle.get("reconciled") is False
    assert lifecycle.get("deferred_reason")


# ---------------------------------------------------------------------------
# T10 - profile delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_delete_hands_shared_modules_on_then_the_last_profile_retires_them(
    stores, writer, tmp_path,
):
    """pa_99 (repos A1: viin_ai + viin_ai_rag, A2: viin_ai) and pb_99 (B: viin_ai).
    Deleting pa_99 retires viin_ai_rag and leaves viin_ai to pb_99 alone; then
    deleting pb_99 retires viin_ai. Every ledger row ends retired/repo_removed
    with repo_id NULL."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="a1",
          modules=(AI, RAG), head="h1")
    _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="a2",
          modules=(AI,), head="h2")
    _repo(tmp_path, writer, profile_id=pb, profile="pb_99", basename="b",
          modules=(AI,), head="h3")
    for m, p in ((AI, "pa_99"), (RAG, "pa_99"), (AI, "pb_99")):
        ls.seed_embedding(m, p)

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/profiles/{pa}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_name"] == "pa_99"
        assert resp.json()["repo_count"] == 2

        assert ls.module_node(driver, RAG) is None
        assert ls.subtree(driver, RAG) == {}
        assert ls.module_node(driver, AI)["profile"] == ["pb_99"]
        assert ls.module_node(driver, AI)["repos"] == ["b"]
        assert ls.embedding_groups(conn) == {(AI, V, "pb_99"): 1}
        pa_rows = ls.ledger_rows(conn, "profile_name = 'pa_99'")
        assert sorted((r["repo_basename"], r["name"]) for r in pa_rows) == [
            ("a1", AI), ("a1", RAG), ("a2", AI)]
        assert all(
            (r["state"], r["retire_reason"], r["repo_id"]) == ("retired", "repo_removed", None)
            for r in pa_rows
        ), pa_rows
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM profiles WHERE id = %s", (pa,))
            assert cur.fetchone()[0] == 0

        resp = await client.delete(f"/api/repos/profiles/{pb}")
        assert resp.status_code == 200, resp.text

    assert ls.module_node(driver, AI) is None
    assert ls.subtree(driver, AI) == {}
    assert ls.embedding_groups(conn) == {}
    rows = ls.ledger_rows(conn)
    assert len(rows) == 4
    assert all((r["state"], r["repo_id"]) == ("retired", None) for r in rows), rows


# ---------------------------------------------------------------------------
# F21 - a busy lock changes nothing
# ---------------------------------------------------------------------------


def _pg_snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT id, profile_id, local_path, head_sha, presence_head_sha "
                    "FROM repos ORDER BY id")
        repos = cur.fetchall()
        cur.execute("SELECT id, name FROM profiles ORDER BY id")
        profiles = cur.fetchall()
    ledger = [
        {k: str(v) for k, v in r.items()} for r in ls.ledger_rows(conn)
    ]
    return {"repos": repos, "profiles": profiles, "ledger": ledger,
            "embeddings": ls.embedding_groups(conn)}


@pytest.fixture
def busy_budget(monkeypatch):
    """Shrink the Web UI lock budget so a held lock times out in ~1s."""
    monkeypatch.setattr("src.constants.WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS", 1.0, raising=False)
    from src.db.repo_registry import RepoStore
    if RepoStore.update_profile.__kwdefaults__ is not None:
        monkeypatch.setitem(RepoStore.update_profile.__kwdefaults__, "lock_wait_seconds", 1.0)


def _hold(lock_id: int):
    from src.db.pg import get_pool

    holder = psycopg2.connect(get_pool().dsn)
    holder.autocommit = True
    with holder.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
    return holder


def _lock_ids(kind: str, *, repo_id: int = 0, profile: str = "") -> int:
    if kind == "ledger":
        from src.db.module_presence import retire_lock_id
        return retire_lock_id(V)
    if kind == "git":
        from src.indexer.pipeline import _repo_lock_id
        return _repo_lock_id(repo_id)
    raise AssertionError(kind)


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "lock"), [
    ("repo", "ledger"), ("repo", "git"), ("profile", "ledger"), ("profile", "git"),
])
async def test_a_removal_that_cannot_take_its_locks_answers_409_and_changes_nothing(
    stores, writer, tmp_path, busy_budget, target, lock,
):
    """An index run's reconcile holds retire:99.0 (or a clone holds the repo's git
    lock): the delete answers 409 with Retry-After and leaves the repo/profile,
    every ledger row, every node and every embedding exactly as they were."""
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    rid = _repo(tmp_path, writer, profile_id=pa, profile="pa_99", basename="addons_a",
                modules=(AI, RAG), head="h1")
    ls.seed_embedding(RAG, "pa_99")
    pg_before, graph_before = _pg_snapshot(conn), ls.graph_snapshot(driver)
    assert graph_before and pg_before["ledger"], "positive control"

    holder = _hold(_lock_ids(lock, repo_id=rid))
    try:
        url = f"/api/repos/repos/{rid}" if target == "repo" else f"/api/repos/profiles/{pa}"
        async with _client(create_app()) as client:
            resp = await client.delete(url)
    finally:
        holder.close()

    assert resp.status_code == 409, resp.text
    assert resp.headers.get("retry-after") == "60"
    assert resp.json().get("error")
    assert _pg_snapshot(conn) == pg_before
    assert ls.graph_snapshot(driver) == graph_before


# ---------------------------------------------------------------------------
# L7 - profile rename
# ---------------------------------------------------------------------------


def _seed_history(conn, profile_id: int, profile: str, tmp_path) -> None:
    """Ledger history of a repo removed from *profile* (rows kept, repo_id NULL)."""
    rid = ls.add_repo(profile_id, tmp_path / profile / "old_addons")
    ls.observe(rid, profile, (AI, RAG), head="h1")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repos WHERE id = %s", (rid,))


@pytest.mark.asyncio
async def test_renaming_a_profile_keeps_its_ledger_history_readable_under_the_new_name(
    stores, tmp_path,
):
    conn, _ = stores
    pid = ls.add_profile("old_name_99")
    _seed_history(conn, pid, "old_name_99", tmp_path)
    store = ls.presence_store()
    assert len(store.lifecycle_rows(RAG, V, ["old_name_99"])) == 1, "positive control"

    async with _client(create_app()) as client:
        resp = await client.patch(f"/api/repos/profiles/{pid}", json={"name": "new_name_99"})
    assert resp.status_code == 200, resp.text

    for name in (AI, RAG):
        assert len(store.lifecycle_rows(name, V, ["new_name_99"])) == 1
        assert store.lifecycle_rows(name, V, ["old_name_99"]) == []


@pytest.mark.asyncio
async def test_a_rename_behind_a_busy_ledger_lock_moves_neither_the_name_nor_the_ledger(
    stores, tmp_path, busy_budget,
):
    conn, _ = stores
    pid = ls.add_profile("old_name_99")
    _seed_history(conn, pid, "old_name_99", tmp_path)
    before = _pg_snapshot(conn)

    holder = _hold(_lock_ids("ledger"))
    try:
        async with _client(create_app()) as client:
            resp = await client.patch(f"/api/repos/profiles/{pid}",
                                      json={"name": "new_name_99"})
    finally:
        holder.close()

    assert resp.status_code == 409, resp.text
    assert resp.headers.get("retry-after")
    assert _pg_snapshot(conn) == before
    assert len(ls.presence_store().lifecycle_rows(RAG, V, ["old_name_99"])) == 1


# ---------------------------------------------------------------------------
# L3 - INHERITS re-link after a retirement
# ---------------------------------------------------------------------------


def _seed_two_definers_and_an_extender(driver) -> None:
    """``x.doc`` defined by def_a (repo A) and def_b (repo B); ext_b extends it
    but was written before def_b existed, so it only points at def_a (the
    cross-repo write-order gap reconcile_same_name_inherits fills)."""
    with driver.session() as s:
        s.run(
            """
            UNWIND [['def_a', 'addons_a', 'pa_99'], ['def_b', 'addons_b', 'pb_99'],
                    ['ext_b', 'addons_b', 'pb_99']] AS row
            MERGE (m:Module {name: row[0], odoo_version: $v})
            SET m.repo = row[1], m.repos = [row[1]], m.profile = [row[2]], m.path = row[0]
            MERGE (x:Model {name: 'x.doc', module: row[0], odoo_version: $v})
            SET x.is_definition = row[0] STARTS WITH 'def', x.profile = [row[2]]
            MERGE (x)-[:DEFINED_IN]->(m)
            """,
            v=V,
        )
        s.run(
            "MATCH (e:Model {name: 'x.doc', module: 'ext_b', odoo_version: $v}), "
            "(d:Model {name: 'x.doc', module: 'def_a', odoo_version: $v}) "
            "MERGE (e)-[:INHERITS]->(d)",
            v=V,
        )


def _extender_targets(driver) -> set[str]:
    with driver.session() as s:
        return {r["m"] for r in s.run(
            "MATCH (:Model {name: 'x.doc', module: 'ext_b', odoo_version: $v})"
            "-[:INHERITS]->(d:Model) RETURN d.module AS m", v=V,
        ).data()}


@pytest.mark.asyncio
async def test_retiring_a_definer_through_repo_delete_relinks_its_extenders(stores, tmp_path):
    conn, driver = stores
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = ls.add_repo(pa, tmp_path / "pa_99" / "addons_a")
    rid_b = ls.add_repo(pb, tmp_path / "pb_99" / "addons_b")
    ls.observe(rid_a, "pa_99", ("def_a",), head="h1")
    ls.observe(rid_b, "pb_99", ("def_b", "ext_b"), head="h2")
    _seed_two_definers_and_an_extender(driver)
    assert _extender_targets(driver) == {"def_a"}, "positive control: the gap exists"

    async with _client(create_app()) as client:
        resp = await client.delete(f"/api/repos/repos/{rid_a}")
    assert resp.status_code == 200, resp.text

    assert ls.module_node(driver, "def_a") is None
    assert _extender_targets(driver) == {"def_b"}


def _pending_definer(conn, tmp_path):
    """def_a's only repo dropped it (retire_pending 'absent'); B keeps def_b + ext_b."""
    pa = ls.add_profile("pa_99")
    pb = ls.add_profile("pb_99")
    rid_a = ls.add_repo(pa, tmp_path / "pa_99" / "addons_a")
    rid_b = ls.add_repo(pb, tmp_path / "pb_99" / "addons_b")
    ls.observe(rid_a, "pa_99", ("def_a", "keep_a"), head="h1")
    ls.observe(rid_b, "pb_99", ("def_b", "ext_b"), head="h2")
    ls.presence_store().mark_retire_pending(rid_a, ["def_a"], "absent")


def test_reconcile_without_the_sweep_still_relinks_after_retiring_a_definer(
    stores, writer, tmp_path,
):
    """A skip night (sweep=False) that retires a definer must re-link its
    extenders to the remaining definition (L3), not leave them dangling."""
    from src.indexer.reconcile import reconcile_version

    conn, driver = stores
    _seed_two_definers_and_an_extender(driver)
    _pending_definer(conn, tmp_path)
    started = writer.server_now() + timedelta(seconds=1)

    report = reconcile_version(V, writer=writer, run_started_at=started,
                               store=ls.presence_store(), sweep=False)

    assert "def_a" in report.retired
    assert ls.module_node(driver, "def_a") is None
    assert _extender_targets(driver) == {"def_b"}


@pytest.mark.parametrize("mode", ["dry_run", "no_retire"])
def test_a_reconcile_that_deletes_nothing_never_relinks(stores, writer, tmp_path, mode):
    # GUARD: pre-existing behaviour (dry run / --no-retire never wrote the graph).
    from src.indexer.reconcile import reconcile_version

    conn, driver = stores
    _seed_two_definers_and_an_extender(driver)
    _pending_definer(conn, tmp_path)
    started = writer.server_now() + timedelta(seconds=1)

    reconcile_version(V, writer=writer, run_started_at=started, store=ls.presence_store(),
                      sweep=False, dry_run=(mode == "dry_run"),
                      retire=(mode != "no_retire"))

    assert ls.module_node(driver, "def_a") is not None
    assert _extender_targets(driver) == {"def_a"}


# ---------------------------------------------------------------------------
# Repo JSON lifecycle fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_json_reports_the_ledger_state_of_each_repo(stores, tmp_path):
    """/profiles and /clone-status carry presence_head_sha, lifecycle_counts and
    the attention text + time for an admin."""
    conn, _ = stores
    pid = ls.add_profile("pa_99")
    rid = ls.add_repo(pid, tmp_path / "pa_99" / "addons_a")
    ls.observe(rid, "pa_99", ("m1", "m2", "m3"), head="h7",
               excluded={"m4": "installable_false"})
    store = ls.presence_store()
    store.mark_retire_pending(rid, ["m3"], "absent")
    store.mark_needs_rewrite(rid, "m1")
    store.record_orphan_retired(
        rid, "m5", profile_name="pa_99", odoo_version=V, path="m5",
        manifest_file="m5/__manifest__.py", last_seen_sha="h0",
        last_seen_at="2026-01-01T00:00:00+00:00",
    )
    store.set_lifecycle_attention(rid, "gate G-B tripped: 3 of 4 modules vanished")

    async with _client(create_app()) as client:
        listing = (await client.get("/api/repos/profiles")).json()
        status = (await client.get(f"/api/repos/repos/{rid}/clone-status")).json()

    [repo] = [r for p in listing["profiles"] for r in p["repos"] if r["id"] == rid]
    for entry in (repo, status):
        assert entry.get("presence_head_sha") == "h7"
        assert entry.get("lifecycle_counts") == {
            "present": 3, "excluded": 1, "retired": 1, "retire_pending": 1,
            "needs_rewrite": 1,
        }
        assert entry.get("lifecycle_attention") == "gate G-B tripped: 3 of 4 modules vanished"
        assert entry.get("lifecycle_attention_at") is not None
    assert status.get("head_sha") == "h7"
    json.dumps(listing)  # JSON-safe end to end
