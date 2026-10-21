# SPDX-License-Identifier: AGPL-3.0-or-later
"""Moved and merged modules end equal to git after the source-oracle prune (ADR-0056/0057).

Question answered here: earlier PRs handled "module X merged into module Y"
and "module moved from one repo to another"; the source-oracle prune
(ADR-0057) now decides every entity deletion from the module's HEAD tree. The
rules protected here, each with a module that carries one artifact of every
kind (models, fields, methods, views, a report, a JS patch, an OWL component,
a stylesheet, a JS test suite, Python test classes; ``_retirement_fixture``):

* a module moved to another repo (same profile or another profile, either
  repo indexed first, the source repo nesting it one directory deeper) ends
  owned by the new repo only: every child the new copy defines is live, every
  entity only the old copy had is gone with its embedding rows, no child keeps
  the old repo's file path, the old profile keeps no embedding row and no child
  carries its profile (no tenant leak), and nothing is recorded as a parser gap;
* a move whose target is not synced yet keeps the module (undecidable) and
  converges once the target syncs;
* a move to a repo where the module is ``installable: False`` retires it from
  the graph and the embeddings (nobody ships it installable);
* a module moved to another directory of its repo keeps every child and
  drops every node of the old path;
* a merge across repos retires X without naming a successor from git;
* a same-name fork dropped by one repo keeps the other owner's data and loses
  what only the dropping copy defined;
* a repo row re-pointed to another repository retires what the new tree does
  not ship and keeps what it does.

Every test drives the REAL ``index_profile`` / ``index_all`` over temp git repos
with a bare origin, against a real Neo4j and PostgreSQL + pgvector. Expected
values come from the rules above, never from what the implementation returns.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.indexer.embedder import FakeEmbedder
from src.indexer.pipeline import index_all
from tests import _retirement_fixture as rf
from tests._lifecycle_repo import (
    GitRepo,
    V,
    children,
    embeddings,
    lc,
    ledger,
    module_node,
    needs_attention,
    register,
    repo_row,
    run,
    run_git,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

X = rf.RETIRED  # "viin_ai_rag": the module that moves / is merged
Y = rf.SURVIVOR  # "viin_ai": the merge target

# Entities of X the moved copy no longer defines (the move commit also
# drops rag_count and its compute method).
_ONLY_OLD_COPY = [
    ("Field", {"model": "ai.assistant", "name": "rag_count"}),
    ("Method", {"model": "ai.assistant", "name": "_compute_rag_count"}),
]
# One child of every file-keyed or file-scoped kind, as the new copy defines it.
_FILE_KEYED = ("Stylesheet", "JsTestSuite", "JSPatch", "TestClass", "TestMethod")


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def _count(driver, label: str, module: str, **props) -> int:
    where = "".join(f" AND n.{k} = ${k}" for k in props)
    with driver.session() as s:
        return s.run(
            f"MATCH (n:{label}) WHERE n.odoo_version = $v AND n.module = $m{where} "
            "RETURN count(n) AS n",
            v=V, m=module, **props,
        ).single()["n"]


def _leftovers(driver, module: str, profile: str | None = None) -> list[tuple]:
    """Nodes naming *module* (optionally: those carrying *profile*).

    Shared AssetBundle nodes are included on purpose: the reconcile resets
    their ``module`` / ``profile`` to the live contributors (ADR-0057 R9), so
    a bundle may name the module only while it contributes, and carry only
    its contributors' profiles."""
    with driver.session() as s:
        rows = s.run(
            "MATCH (n) WHERE n.odoo_version = $v AND n.module = $m "
            "AND NOT n:Module "
            "AND ($p IS NULL OR $p IN coalesce(n.profile, [])) "
            "RETURN labels(n)[0] AS l, coalesce(n.name, n.xmlid, n.file_path) AS k, "
            "n.file_path AS f, n.profile AS p ORDER BY l, k",
            v=V, m=module, p=profile,
        ).data()
    return [(r["l"], r["k"], r["f"], tuple(r["p"] or ())) for r in rows]


def _bundle(driver, name: str) -> tuple[str | None, tuple[str, ...]] | None:
    """(module, sorted profile) of the AssetBundle *name*, None when absent."""
    with driver.session() as s:
        rec = s.run(
            "MATCH (b:AssetBundle {name: $n, odoo_version: $v}) "
            "RETURN b.module AS m, coalesce(b.profile, []) AS p",
            n=name, v=V,
        ).single()
    return (rec["m"], tuple(sorted(rec["p"]))) if rec else None


def _child_profiles(driver, module: str) -> set[tuple[str, ...]]:
    return {tuple(sorted(p)) for _l, _k, _f, p in _leftovers(driver, module)}


def _assert_gone(driver, pg_conn, module: str) -> None:
    assert module_node(driver, module) is None, f"{module}: Module node must be retired"
    assert not _leftovers(driver, module), _leftovers(driver, module)
    assert embeddings(pg_conn, module) == 0, f"{module}: every embedding row must be retired"


def _file_paths(driver, module: str) -> set[str]:
    """File paths of every child node of *module* that carries one."""
    with driver.session() as s:
        rows = s.run(
            "MATCH (n) WHERE n.odoo_version = $v AND n.module = $m AND NOT n:Module "
            "AND n.file_path IS NOT NULL RETURN DISTINCT n.file_path AS f",
            v=V, m=module,
        ).data()
    return {r["f"] for r in rows}


def _emb_keys(pg_conn, module: str, profile: str | None = None) -> set[tuple[str, str]]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_type, entity_name FROM embeddings WHERE odoo_version = %s "
            "AND module = %s AND (%s::text IS NULL OR profile_name = %s::text)",
            (V, module, profile, profile),
        )
        return {(r[0], r[1]) for r in cur.fetchall()}


def _emb_file_paths(pg_conn, module: str) -> set[str]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT file_path FROM embeddings WHERE odoo_version = %s "
            "AND module = %s AND file_path IS NOT NULL",
            (V, module),
        )
        return {r[0] for r in cur.fetchall()}


def _no_parser_gap(driver, module: str) -> None:
    node = module_node(driver, module) or {}
    gap = {k: v for k, v in node.items() if k.startswith(("parser_gap_", "prune_held_"))}
    assert not gap, f"{module}: nothing may be kept as a parser gap after a move: {gap}"


def _assert_moved_copy_complete(driver, pg_conn, module: str, new_prefix: str,
                                *, profile: str) -> None:
    """The new copy is fully live, nothing of the old copy is left."""
    assert module_node(driver, module) is not None, f"{module}: Module node must stay"
    for label in _FILE_KEYED:
        assert _count(driver, label, module) >= 1, f"{module}: its {label} must be live"
    assert _count(driver, "Stylesheet", module) == 1, "exactly the new copy's stylesheet"
    assert _count(driver, "JsTestSuite", module) == 1, "exactly the new copy's JS suite"
    assert _count(driver, "TestClass", module, name="TestRagSource") == 1
    assert _count(driver, "TestMethod", module, name="test_reindex_returns_true") == 1
    assert _count(driver, "Model", module, name="ai.rag.source") == 1
    for label, props in _ONLY_OLD_COPY:
        assert _count(driver, label, module, **props) == 0, (
            f"{label} {props} only the old copy defined must be gone")
    stray = {p for p in _file_paths(driver, module) if not p.startswith(new_prefix)}
    assert not stray, f"{module}: children still carry the old copy's paths: {stray}"
    stray_rows = {p for p in _emb_file_paths(pg_conn, module) if not p.startswith(new_prefix)}
    assert not stray_rows, f"{module}: embedding rows of the old copy's paths: {stray_rows}"
    keys = _emb_keys(pg_conn, module, profile)
    assert ("field", "ai.assistant.rag_count") not in keys
    assert ("field", "ai.rag.source.name") in keys
    _no_parser_gap(driver, module)


# ---------------------------------------------------------------------------
# Repo builders
# ---------------------------------------------------------------------------

def _source_repo(parent: Path, name: str, *, nested: bool) -> tuple[GitRepo, Path]:
    """viin_ai + viin_ai_rag (every artifact kind), the latter under addons/ when
    *nested* (Odoo CE layout) or at the root (addon repo layout)."""
    repo = GitRepo(parent, name)
    rf.write_viin_ai(repo.path)
    root = repo.path / "addons" if nested else repo.path
    rf.write_viin_ai_rag(root)
    repo.commit("add viin_ai and viin_ai_rag")
    return repo, root


def _move_x(src: GitRepo, src_root: Path, dst: GitRepo, dst_root: Path | None = None,
            *, installable: bool = True) -> None:
    """Move X from *src* to *dst* (both commits pushed); the moved copy drops
    rag_count and its compute method."""
    dst_root = dst_root or dst.path
    shutil.copytree(src_root / X, dst_root / X)
    models = dst_root / X / "models" / "ai_rag_source.py"
    models.write_text(rf._RAG_MODELS.split("    rag_count = fields.Integer(")[0])
    if not installable:
        manifest = dst_root / X / "__manifest__.py"
        manifest.write_text(manifest.read_text().replace("'installable': True",
                                                         "'installable': False"))
    src.rm(str((src_root / X).relative_to(src.path)))
    src.commit(f"[MOV] {X} to {dst.path.name}")
    dst.commit(f"[MOV] {X} from {src.path.name}")


# ---------------------------------------------------------------------------
# M1 / M2 - moved between repos (same profile, other profile)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nested", [False, True], ids=["same_layout", "nested_source"])
@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_module_moved_between_repos_of_one_profile_ends_owned_by_the_target_only(
    pg, neo4j_driver, tmp_path, order, nested,
):
    """M1: X moves from repo A to repo B of the same profile. Whatever repo the
    run indexes first, after the night and the survivor's re-write X is B's:
    every artifact of B's copy is live, nothing of A's copy is left (no child
    and no embedding row on A's paths, the field only A's copy had is gone),
    no parser gap, and the next night is a zero-cost skip."""
    a, a_root = _source_repo(tmp_path / "a", "repo_a", nested=nested)
    b = GitRepo(tmp_path / "b", "repo_b")
    write_module(b, "b_keep")
    b.commit("b")
    repos = (a, b) if order == "source_first" else (b, a)
    ids = dict(zip((r.path.name for r in repos), register("move_99", *repos)))
    run(pg, "move_99")
    assert module_node(neo4j_driver, X)["repo"] == "repo_a"
    _move_x(a, a_root, b)

    first = run(pg, "move_99")
    second = run(pg, "move_99")
    third = run(pg, "move_99")

    for summary in (first, second):
        assert not needs_attention(summary), lc(summary)
    node = module_node(neo4j_driver, X)
    assert node["repo"] == "repo_b" and node["profile"] == ["move_99"]
    assert ledger(pg, ids["repo_a"], X)["state"] == "retired"
    assert ledger(pg, ids["repo_b"], X)["state"] == "present"
    assert ledger(pg, ids["repo_b"], X)["needs_rewrite"] is False
    _assert_moved_copy_complete(neo4j_driver, pg, X, f"{X}/", profile="move_99")
    assert third["modules"] == 0, "converged: the next night re-parses nothing"


@pytest.mark.parametrize("nested", [False, True], ids=["same_layout", "nested_source"])
@pytest.mark.parametrize("order", ["source_first", "target_first", "index_all"])
def test_module_moved_to_another_profiles_repo_leaves_nothing_in_the_old_profile(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, order, nested,
):
    """M2: X moves from tenant A's repo (profile a_99) to tenant B's repo
    (profile b_99). After convergence the graph keeps X (B ships it) owned by
    b_99 only - the Module and every child - a_99 keeps no embedding row of X
    (no leak through A's profile), b_99 has them, and nothing of A's copy or
    only in A's copy survives."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    a, a_root = _source_repo(tmp_path / "a", "repo_a", nested=nested)
    b = GitRepo(tmp_path / "b", "repo_b")
    write_module(b, "b_keep")
    b.commit("b")
    (rid_a,) = register("a_99", a)
    (rid_b,) = register("b_99", b)
    run(pg, "a_99")
    run(pg, "b_99")
    assert embeddings(pg, X, "a_99") > 0, "precondition"
    _move_x(a, a_root, b)

    for _night in range(2):
        if order == "index_all":
            summary = index_all(pg, embedder=FakeEmbedder(dim=1024), profile_workers=2)
            assert summary["profiles_failed"] == []
            assert not needs_attention(summary), lc(summary)
        else:
            first, second = ("a_99", "b_99") if order == "source_first" else ("b_99", "a_99")
            for profile in (first, second):
                assert not needs_attention(run(pg, profile)), profile

    node = module_node(neo4j_driver, X)
    assert node is not None and node["profile"] == ["b_99"]
    assert _child_profiles(neo4j_driver, X) == {("b_99",)}, (
        "no child keeps a_99", _leftovers(neo4j_driver, X, "a_99"))
    assert _bundle(neo4j_driver, "web.assets_backend") == (X, ("b_99",)), (
        "the shared bundle keeps no profile of the departed contributor")
    assert embeddings(pg, X, "a_99") == 0, "a_99 keeps no row of the moved module"
    assert embeddings(pg, X, "b_99") > 0
    assert ledger(pg, rid_a, X)["state"] == "retired"
    assert ledger(pg, rid_b, X)["state"] == "present"
    _assert_moved_copy_complete(neo4j_driver, pg, X, f"{X}/", profile="b_99")


def _with_rng(repo: GitRepo) -> None:
    """Ship the core RelaxNG schemas so views get linted (LintViolation exists)."""
    rng = repo.path / "odoo" / "addons" / "base" / "rng"
    rng.mkdir(parents=True, exist_ok=True)
    for f in rf.RNG_DIR.iterdir():
        shutil.copy(f, rng / f.name)


def _lint_paths(driver) -> set[str]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (lv:LintViolation {odoo_version: $v}) RETURN DISTINCT lv.file_path AS f",
            v=V,
        ).data()
    return {r["f"] for r in rows}


@pytest.mark.parametrize("profiles", ["one_profile", "two_profiles"])
@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_lint_violations_of_the_old_nested_copy_leave_after_a_move(
    pg, neo4j_driver, tmp_path, order, profiles,
):
    """M1/M2 lint: the old copy (repo A, ``addons/<m>``) raised a violation in
    its list view; after X moves to repo B (``<m>`` at the root) the violation
    is raised in B's file only. The old copy's LintViolation carries A's
    path, which lies under no directory of X in B: it must still leave (git
    proves B's HEAD has nothing there) instead of being served for ever."""
    a, a_root = _source_repo(tmp_path / "a", "repo_a", nested=True)
    _with_rng(a)
    a.commit("rng")
    b = GitRepo(tmp_path / "b", "repo_b")
    write_module(b, "b_keep")
    _with_rng(b)
    b.commit("b")
    names = ("a_99", "b_99") if profiles == "two_profiles" else ("ab_99", "ab_99")
    ordered = [(names[0], a), (names[1], b)]
    if order == "target_first":
        ordered.reverse()
    for profile, repo in ordered:
        register(profile, repo)
    for profile in dict.fromkeys(p for p, _r in ordered):
        run(pg, profile)
    old = f"addons/{X}/views/ai_rag_source_views.xml"
    assert old in _lint_paths(neo4j_driver), "precondition: the old copy is linted"
    _move_x(a, a_root, b)

    for _night in range(2):
        for profile in dict.fromkeys(p for p, _r in ordered):
            assert not needs_attention(run(pg, profile)), profile

    assert _lint_paths(neo4j_driver) == {f"{X}/views/ai_rag_source_views.xml"}
    _no_parser_gap(neo4j_driver, X)


# ---------------------------------------------------------------------------
# M3 - the target is not synced yet
# ---------------------------------------------------------------------------

def test_move_to_a_repo_not_synced_yet_keeps_the_module_until_the_target_syncs(
    pg, neo4j_driver, tmp_path,
):
    """M3: X moves to repo B, which is registered and cloned (its HEAD tracks
    X) but has never been indexed. A's run cannot know whether B takes X, so X
    is kept (undecidable, attention); B's first run takes X; A's next run
    leaves X to B with nothing of A's copy left."""
    a, a_root = _source_repo(tmp_path / "a", "repo_a", nested=False)
    b = GitRepo(tmp_path / "b", "repo_b")
    write_module(b, "b_keep")
    b.commit("b")
    (rid_a,) = register("a_99", a)
    run(pg, "a_99")
    _move_x(a, a_root, b)
    (rid_b,) = register("b_99", b)

    held = run(pg, "a_99")

    assert module_node(neo4j_driver, X) is not None, "nothing deleted while B may ship X"
    assert children(neo4j_driver, X) > 0 and embeddings(pg, X) > 0
    assert any(X in u for u in lc(held).get("undecidable", [])), lc(held)
    assert ledger(pg, rid_a, X)["retire_pending"] is True

    run(pg, "b_99")
    run(pg, "a_99")
    run(pg, "b_99")

    node = module_node(neo4j_driver, X)
    assert node is not None and node["profile"] == ["b_99"]
    assert _child_profiles(neo4j_driver, X) == {("b_99",)}, _leftovers(neo4j_driver, X, "a_99")
    assert embeddings(pg, X, "a_99") == 0
    assert ledger(pg, rid_a, X)["state"] == "retired"
    assert ledger(pg, rid_b, X)["state"] == "present"
    _assert_moved_copy_complete(neo4j_driver, pg, X, f"{X}/", profile="b_99")


# ---------------------------------------------------------------------------
# M4 - moved with installable False at the target
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_module_moved_to_a_repo_that_ships_it_not_installable_leaves_the_index(
    pg, neo4j_driver, tmp_path, order,
):
    """M4 (tvtmaaddons -> erponline-enterprise 18/19, 676cf82849): the target
    ships X with installable False. Nobody ships X installable any more, so X
    leaves the graph and every profile's embeddings; A's row is retired, B's
    row records the exclusion; no gate, no attention."""
    a, a_root = _source_repo(tmp_path / "a", "repo_a", nested=False)
    b = GitRepo(tmp_path / "b", "repo_b")
    write_module(b, "b_keep")
    b.commit("b")
    (rid_a,) = register("a_99", a)
    (rid_b,) = register("b_99", b)
    run(pg, "a_99")
    run(pg, "b_99")
    _move_x(a, a_root, b, installable=False)

    profiles = ("a_99", "b_99") if order == "source_first" else ("b_99", "a_99")
    for _night in range(2):
        for profile in profiles:
            summary = run(pg, profile)
            assert not needs_attention(summary), (profile, lc(summary))

    _assert_gone(neo4j_driver, pg, X)
    assert ledger(pg, rid_a, X)["state"] == "retired"
    row = ledger(pg, rid_b, X)
    assert (row["state"], row["exclusion_reason"]) == ("excluded", "installable_false")
    assert module_node(neo4j_driver, Y) is not None, "the source's other module stays"


# ---------------------------------------------------------------------------
# M5 - moved to another directory of the same repo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction", ["into_subdir", "out_of_subdir"])
def test_module_moved_to_another_directory_of_its_repo_keeps_every_child(
    pg, neo4j_driver, tmp_path, direction,
):
    """M5: X moves inside its repo (root <-> addons/). Every artifact stays live,
    no child or embedding row keeps the old path, nothing is a parser gap."""
    nested_before = direction == "out_of_subdir"
    repo, root = _source_repo(tmp_path, "repo_a", nested=nested_before)
    (rid,) = register("a_99", repo)
    run(pg, "a_99")
    old_rel = str((root / X).relative_to(repo.path))
    new_rel = X if nested_before else f"addons/{X}"
    (repo.path / new_rel).parent.mkdir(parents=True, exist_ok=True)
    run_git(repo.path, "mv", old_rel, new_rel)
    models = repo.path / new_rel / "models" / "ai_rag_source.py"
    models.write_text(rf._RAG_MODELS.split("    rag_count = fields.Integer(")[0])
    repo.commit(f"[MOV] {X}: move to {new_rel}")

    first = run(pg, "a_99")
    run(pg, "a_99")

    assert not needs_attention(first), lc(first)
    assert ledger(pg, rid, X)["state"] == "present"
    assert module_node(neo4j_driver, X)["path"].rstrip("/").endswith(new_rel)
    _assert_moved_copy_complete(neo4j_driver, pg, X, f"{new_rel}/", profile="a_99")


# ---------------------------------------------------------------------------
# M6 - N:1 merge in one repo
# ---------------------------------------------------------------------------

_EXT = "viin_ai_rag_ext"


# ---------------------------------------------------------------------------
# M7 - merge across repos
# ---------------------------------------------------------------------------

def test_merge_across_repos_retires_x_and_names_no_successor_from_the_other_repo(
    pg, neo4j_driver, tmp_path,
):
    """M7: X is deleted from repo A and its model lands in Y in repo B. X is
    retired everywhere; git of A cannot see B, so no successor is named; Y in B
    defines ai.rag.source and the extender in A relinks to it."""
    a = GitRepo(tmp_path / "a", "repo_a")
    rf.write_viin_ai_rag(a.path)
    write_module(a, _EXT, depends=[Y], inherit="ai.rag.source")
    a.commit("a")
    b = GitRepo(tmp_path / "b", "repo_b")
    rf.write_viin_ai(b.path)
    b.commit("b")
    (rid_a, _rid_b) = register("ab_99", a, b)
    run(pg, "ab_99")
    shutil.copy(a.path / X / "models" / "ai_rag_source.py",
                b.path / Y / "models" / "ai_rag_source.py")
    (b.path / Y / "models" / "__init__.py").write_text(
        "from . import ai_assistant\nfrom . import ai_rag_source\n")
    b.commit(f"[MERGE] {X} into {Y}")
    a.rm(X)
    a.commit(f"[REM] {X}: merged into {Y} (repo_b)")

    summary = run(pg, "ab_99")
    run(pg, "ab_99")

    assert not needs_attention(summary), lc(summary)
    _assert_gone(neo4j_driver, pg, X)
    row = ledger(pg, rid_a, X)
    assert row["state"] == "retired"
    assert not row["successor_names"], row["successor_names"]
    assert _count(neo4j_driver, "Model", Y, name="ai.rag.source") == 1
    with neo4j_driver.session() as s:
        targets = s.run(
            "MATCH (e:Model {name: 'ai.rag.source', module: $ext, odoo_version: $v})"
            "-[:INHERITS]->(d:Model {name: 'ai.rag.source'}) RETURN collect(d.module) AS m",
            ext=_EXT, v=V,
        ).single()["m"]
    assert targets == [Y], targets


# ---------------------------------------------------------------------------
# M8 - same-name fork dropped by one owner
# ---------------------------------------------------------------------------

def test_fork_dropped_by_one_repo_keeps_the_other_copy_and_loses_what_only_it_had(
    pg, neo4j_driver, tmp_path,
):
    """M8: X is shipped by two repos (two profiles); fork A carries an extra
    field. A drops X: the node stays owned by B only, B's entities stay, the
    field only A's copy defined goes (graph + rows), a_99 keeps nothing."""
    a = GitRepo(tmp_path / "a", "fork_a")
    b = GitRepo(tmp_path / "b", "fork_b")
    write_module(a, "forked_mod", extra_field="fork_a_only = fields.Char()")
    write_module(a, "a_keep")
    write_module(b, "forked_mod", extra_field="fork_b_only = fields.Char()")
    a.commit("a")
    b.commit("b")
    (rid_a,) = register("a_99", a)
    (rid_b,) = register("b_99", b)
    run(pg, "a_99")
    run(pg, "b_99")
    a.rm("forked_mod")
    a.commit("drop the fork")

    for _night in range(2):
        for profile in ("a_99", "b_99"):
            assert not needs_attention(run(pg, profile)), profile

    node = module_node(neo4j_driver, "forked_mod")
    assert node is not None and node["profile"] == ["b_99"]
    assert _child_profiles(neo4j_driver, "forked_mod") == {("b_99",)}
    assert _count(neo4j_driver, "Field", "forked_mod", name="fork_b_only") == 1
    assert _count(neo4j_driver, "Field", "forked_mod", name="label") == 1
    assert _count(neo4j_driver, "Method", "forked_mod", name="action_touch") == 1
    assert _count(neo4j_driver, "Field", "forked_mod", name="fork_a_only") == 0
    assert embeddings(pg, "forked_mod", "a_99") == 0
    assert not any("fork_a_only" in k[1] for k in _emb_keys(pg, "forked_mod"))
    assert ledger(pg, rid_a, "forked_mod")["state"] == "retired"
    assert ledger(pg, rid_b, "forked_mod")["state"] == "present"
    _no_parser_gap(neo4j_driver, "forked_mod")


# ---------------------------------------------------------------------------
# M10 - a repo row re-pointed to another repository
# ---------------------------------------------------------------------------

def test_repo_row_repointed_to_another_repository_follows_the_new_tree(
    pg, neo4j_driver, tmp_path,
):
    """M10: the operator re-points a registered repo to another repository
    (other URL, other checkout, unrelated history) that ships some of the same
    module names. What the new tree ships stays (re-written from it), what it
    does not ship is retired, nothing is held."""
    old = GitRepo(tmp_path / "old", "addons")
    write_module(old, "common_mod", extra_field="old_only = fields.Char()")
    write_module(old, "old_mod")
    old.commit("old")
    new = GitRepo(tmp_path / "new", "addons")
    write_module(new, "common_mod", extra_field="new_only = fields.Char()")
    write_module(new, "new_mod")
    new.commit("new")
    (rid,) = register("rp_99", old)
    run(pg, "rp_99")
    with pg.cursor() as cur:
        cur.execute("UPDATE repos SET url = %s, local_path = %s WHERE id = %s",
                    (new.url, str(new.path), rid))
    pg.commit()

    summary = run(pg, "rp_99")
    run(pg, "rp_99")

    assert not needs_attention(summary), lc(summary)
    _assert_gone(neo4j_driver, pg, "old_mod")
    assert module_node(neo4j_driver, "new_mod") is not None
    assert _count(neo4j_driver, "Field", "common_mod", name="new_only") == 1
    assert _count(neo4j_driver, "Field", "common_mod", name="old_only") == 0
    assert ledger(pg, rid, "old_mod")["state"] == "retired"
    assert repo_row(pg, rid)["presence_head_sha"] == new.head()
