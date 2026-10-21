# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_scope_choke_shared_core_unit.py
"""WI-1 unit tests — owning-profile stamping unhides shared core (ADR-0034).

Pure-unit (no Neo4j, no Postgres). Two halves:

1. The WRITER/pipeline fix: `_owning_profiles` stamps a node's `profile[]` with
   the SINGLE owning profile of its repo — never the descendant ancestor chain.
   This is the root-cause edit: it stops shared-core nodes (`base`, `sale`) from
   accumulating tenant-private profile names from descendant index runs.

2. The ADR-0034 `all()` tenant choke over the arrays the fixed writer now
   produces. The scope params come from the REAL `src.mcp.server._scope` /
   `session.narrow_scope` (stubbed registry, `tests/_scope_world.py`); the
   choke itself is Cypher, so a node's visibility is evaluated by the
   harness's one Python model of it, pinned to the production predicate by
   `test_choke_matches_server_predicate_string`. MUST-PASS invariants:
     - positive (the reported bug): shared core visible to its base-profile caller
     - discovery preserved: shared core visible to a descendant-private caller
     - isolation: a private node denied to a foreign caller
     - ★ same-name cross-tenant collision stays FAIL-CLOSED (most dangerous mode)

The ancestor-chain narrowing rules of a `profile_name` filter (#385: siblings,
descendants, foreign ancestors, out-of-scope names, registry down, session
pin) are covered by `tests/test_profile_ancestor_scope_unit.py`.
"""
import pytest

from src.indexer.models import ModelInfo, ModuleInfo, ParseResult
from src.indexer.pipeline import _owning_profiles
from src.indexer.writer_neo4j_orm import _write_parse_result
from tests._scope_world import choke, make_world

# --- shared/global profiles (tenant_id IS NULL) and tenant-private profiles ---
SHARED = ["odoo_17", "odoo_18"]  # resolve_tenant_scope -> $shared for everyone
# tenant_id -> own profiles (resolve_tenant_scope -> $own)
OWN = {
    "viindoo": ["viindoo_internal_17", "standard_viindoo_17"],
    "acme": ["acme_17"],
    "globex": ["globex_17"],
}
PARENTS = {
    "odoo_17": None,
    "odoo_18": None,
    "standard_viindoo_17": "odoo_17",
    "viindoo_internal_17": "standard_viindoo_17",
    "acme_17": "odoo_17",
    "globex_17": "odoo_17",
}


@pytest.fixture
def world(monkeypatch):
    w = make_world(monkeypatch, parents=PARENTS, own=OWN, shared=SHARED)
    yield w
    w.session.invalidate_allowed_profiles()


def _scope(world, tenant_id, profile_name=None) -> dict:
    """The REAL `server._scope` params for caller *tenant_id* (None: admin)."""
    world.as_(tenant_id)
    return world.scope(profile_name)




# ---------------------------------------------------------------------------
# 1. The writer/pipeline fix — owning profile, not the descendant chain
# ---------------------------------------------------------------------------

def test_owning_profile_is_run_profile_not_ancestor_chain():
    """Core unit test of the fix: stamp [profile_name], never the chain.

    Even when the index run walks a deep ancestor chain, a node from a repo
    registered under `odoo_17` must be stamped ONLY ['odoo_17'].
    """
    repo = {"id": 1, "url": "git@github.com:Viindoo/odoo.git"}  # no profile_name col
    out = _owning_profiles(repo, profile_name="odoo_17", repo_root_name="odoo")
    assert out == ["odoo_17"], (
        "writer must stamp the OWNING profile, not the descendant ancestor chain"
    )


def test_owning_profile_prefers_repo_row_profile_name():
    """When the repo row carries its own profile_name (get_ancestor_repos), use it.

    Guards correctness even if a future caller mixes repos from several profiles
    in one run — each node still gets ITS repo's owner, not the run's profile.
    """
    repo = {"id": 2, "profile_name": "odoo_17"}
    out = _owning_profiles(repo, profile_name="viindoo_internal_17", repo_root_name="odoo")
    assert out == ["odoo_17"]


def test_owning_profile_single_element_never_empty():
    """F-6: result is always a non-empty single-element list (empty -> fail-open)."""
    # neither repo profile_name nor run profile_name -> repo_root_name fallback
    out = _owning_profiles({"id": 3}, profile_name=None, repo_root_name="some_repo")
    assert out == ["some_repo"]
    assert len(out) == 1


def test_private_repo_stamps_only_its_own_private_profile():
    """A genuinely private repo stamps only its private owning profile."""
    repo = {"id": 4, "url": "git@github.com:Viindoo/viindoo_internal.git"}
    out = _owning_profiles(repo, profile_name="viindoo_internal_17", repo_root_name="viin")
    assert out == ["viindoo_internal_17"]


# ---------------------------------------------------------------------------
# 2. The choke over the arrays the fixed writer now produces (real _scope)
# ---------------------------------------------------------------------------

def test_positive_shared_core_visible_to_base_profile_caller(world):
    """The reported bug: `base` (now ['odoo_17']) IS visible to an odoo_17 caller.

    Pre-fix `base.profile` was ['viindoo_internal_17','standard_viindoo_17',
    'odoo_17'] and the all() choke hid it. Post-fix it's ['odoo_17'].
    """
    base_profile = _owning_profiles(
        {"id": 1, "url": "odoo.git"}, profile_name="odoo_17", repo_root_name="odoo",
    )
    scope = _scope(world, "viindoo", profile_name="odoo_17")  # narrowed to odoo_17
    assert choke(base_profile, scope) is True


def test_discovery_descendant_private_caller_still_sees_shared_core(world):
    """A caller scoped to a descendant PRIVATE profile still resolves shared core.

    Works because odoo_17 ∈ $shared for every tenant — the shared owning name on
    the node lands in the caller's $shared.
    """
    base_profile = _owning_profiles(
        {"id": 1, "url": "odoo.git"}, profile_name="odoo_17", repo_root_name="odoo",
    )
    assert choke(base_profile, _scope(world, "viindoo")) is True  # full boundary
    assert choke(base_profile, _scope(world, "viindoo", "viindoo_internal_17")) is True


def test_isolation_private_node_denied_to_foreign_caller(world):
    """A node owned by standard_viindoo_17 is DENIED to a caller scoped to odoo_17."""
    priv = _owning_profiles(
        {"id": 9, "url": "viindoo_std.git"},
        profile_name="standard_viindoo_17", repo_root_name="viin_std",
    )
    assert priv == ["standard_viindoo_17"]
    # caller is acme tenant narrowed to odoo_17 — does not own standard_viindoo_17
    assert choke(priv, _scope(world, "acme", profile_name="odoo_17")) is False
    assert choke(priv, _scope(world, "acme")) is False


def test_collision_same_name_cross_tenant_stays_fail_closed(world):
    """★ The single most dangerous mode: two tenants ship the same (name,version).

    The MERGE key has no tenant_id (ADR-0034 D2), so the two private repos'
    same-name module converges on ONE node whose union carries BOTH private
    owners. The all() choke must fail-CLOSE it to BOTH tenants — no cross-tenant
    content served.
    """
    # Each tenant's index run stamps its own owner; the writer's ON MATCH UNION
    # then accumulates both onto the single converged node.
    a = _owning_profiles({"id": 11}, profile_name="acme_17", repo_root_name="acct")
    b = _owning_profiles({"id": 12}, profile_name="globex_17", repo_root_name="acct")
    converged = sorted(set(a) | set(b))  # ["acme_17", "globex_17"]
    assert converged == ["acme_17", "globex_17"]

    assert choke(converged, _scope(world, "acme")) is False, "denied to tenant A"
    assert choke(converged, _scope(world, "globex")) is False, "denied to tenant B"
    assert choke(converged, _scope(world, "acme", "acme_17")) is False, "filter never widens"
    # admin still sees it (short-circuit)
    assert choke(converged, _scope(world, None)) is True


def test_admin_unrestricted(world):
    """Admin ($own IS NULL) sees every node regardless of its profile array."""
    scope = _scope(world, None)
    assert scope["own"] is None
    assert choke(["viindoo_internal_17"], scope) is True
    assert choke([], scope) is True  # even empty-profile nodes


def test_empty_profile_node_denied_to_scoped_tenant(world):
    """F-6 vacuous-truth guard: empty profile[] is denied to a scoped tenant."""
    assert choke([], _scope(world, "acme")) is False
    assert choke([], _scope(world, "acme", "acme_17")) is False


def test_cross_store_consistency_neo4j_matches_pgvector_owner(world):
    """WG-3t: the single owning name the fixed Neo4j writer stamps is the SAME
    leaf the pgvector writer stamps (write_module_embeddings profile_name=...).

    The pgvector side stamps the run's `profile_name` scalar per chunk; the fixed
    Neo4j side stamps [profile_name]. Membership over the scalar (the real
    `_effective_allowed` list behind the RLS GUC) and all() over the
    single-element array are equivalent -> no split-brain by construction.
    """
    run_profile = "odoo_17"
    neo4j_arr = _owning_profiles({"id": 1}, profile_name=run_profile, repo_root_name="odoo")
    pgvector_scalar = run_profile  # what write_module_embeddings stamps
    assert neo4j_arr == [pgvector_scalar]
    # both stores grant to the same caller
    assert choke(neo4j_arr, _scope(world, "acme")) is True
    world.as_("acme")
    assert pgvector_scalar in world.srv._effective_allowed(None)  # pgvector membership


# ---------------------------------------------------------------------------
# 3. SHOWSTOPPER #1 regression — the dependency-target MERGE must NOT pollute a
#    shared dep target with the depending run's profile (the ACTUAL re-pollution
#    mechanism that survived the first pass). Pure-unit: a recording fake `tx`
#    captures the exact Cypher + params `_write_parse_result` issues, with no
#    Neo4j. We assert the writer's CONTRACT, not a live graph: stamping a foreign
#    profile onto a referenced node is what re-hid shared core on every reindex.
# ---------------------------------------------------------------------------

class _RecordingTx:
    """Minimal stand-in for a neo4j write-transaction.

    Records every (query, params) pair. `.run(...).single()` returns None so the
    writer's unresolved-INHERITS / dep paths behave as "target not yet indexed"
    (None == no row) without a live DB. Only INHERITS uses `.single()`; the
    dependency MERGE we exercise here does not, so None is safe.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, query, **params):
        self.calls.append((query, params))

        class _Result:
            def single(self_inner):
                return None

            def data(self_inner):
                return []
        return _Result()


def _write_module_with_dep(dep_name: str, run_profile: str) -> _RecordingTx:
    """Drive `_write_parse_result` for a module under `run_profile` that
    `depends: [dep_name]`, with a single owned model (no inherit). Returns the
    recording tx so callers can inspect the issued Cypher."""
    module = ModuleInfo(
        name="viin_sale_extra", odoo_version="17.0",
        repo="viin_sale_extra_repo", path="/tmp/viin_sale_extra",
        depends=[dep_name],
    )
    model = ModelInfo(
        name="sale.order.extra", module="viin_sale_extra", odoo_version="17.0",
    )
    result = ParseResult(module=module, models=[model])
    tx = _RecordingTx()
    # The run is owned by the (single) owning profile per the WI-1 writer fix.
    _write_parse_result(tx, result, profiles=[run_profile])
    return tx


def _dep_target_calls(tx: _RecordingTx, dep_name: str) -> list[tuple[str, dict]]:
    """Cypher calls that MERGE the dependency-target Module `dep_name`."""
    return [
        (q, p) for (q, p) in tx.calls
        if "MERGE (d:Module" in q and p.get("dep") == dep_name
    ]


def test_dependency_merge_does_not_pollute_shared_dep_target():
    """★ SHOWSTOPPER #1 regression: writing a module under profile P with
    `depends: base` must NOT stamp P onto the shared `base` Module node.

    Pre-fix the dep MERGE did `ON CREATE SET d.profile=$profiles / ON MATCH SET
    d.profile = <union>` — so every nightly reindex of a Viindoo module re-unioned
    `standard_viindoo_17` onto `base`, re-hiding shared core via the all() choke
    within 24h. Post-fix the dep MERGE touches ONLY the node identity + edge.
    """
    tx = _write_module_with_dep("base", run_profile="standard_viindoo_17")
    dep_calls = _dep_target_calls(tx, "base")
    assert dep_calls, "expected a dependency-target MERGE for base"

    for query, params in dep_calls:
        # The depending run's profile must NEVER be handed to the dep-target MERGE.
        assert "profiles" not in params, (
            "dependency-target MERGE must not receive the depending run's "
            "profiles param — that is the re-pollution vector"
        )
        # And the query must not set d.profile in any form.
        assert "d.profile" not in query, (
            "dependency-target MERGE must not SET d.profile (neither ON CREATE "
            "nor ON MATCH) — a node's profile is set only by the run that owns it"
        )


def test_dependency_merge_still_creates_node_and_edge():
    """The fix removes profile stamping ONLY — the dep node + DEPENDS_ON edge
    must still be created so inheritance/impact queries keep working."""
    tx = _write_module_with_dep("base", run_profile="standard_viindoo_17")
    dep_calls = _dep_target_calls(tx, "base")
    assert dep_calls, "dependency-target MERGE must still run"
    query = dep_calls[0][0]
    assert "MERGE (d:Module" in query
    assert "DEPENDS_ON" in query


def test_owned_module_still_stamps_run_profile():
    """Control: the run's OWN module node still carries its owning profile, so the
    dep-MERGE fix did not over-correct and strip provenance from owned nodes."""
    tx = _write_module_with_dep("base", run_profile="standard_viindoo_17")
    owned = [
        (q, p) for (q, p) in tx.calls
        if "MERGE (m:Module" in q and p.get("name") == "viin_sale_extra"
    ]
    assert owned, "expected the owned-module MERGE"
    query, params = owned[0]
    assert params.get("profiles") == ["standard_viindoo_17"]
    assert "m.profile = $profiles" in query


def test_profile_less_dep_target_fail_closed_to_scoped_caller(world):
    """F-6 fail-closed end-to-end: a forward-referenced dep target the fix leaves
    profile-less (`[]`) is DENIED to a scoped tenant and STILL visible to admin.

    This ties the writer contract (no foreign stamp) to the read-side choke: a
    placeholder/dep target never indexed under its own profile stays []-profiled
    and the `size(profile)>0` guard denies it to scoped callers (no fail-open),
    while admin ($own IS NULL) still sees it."""
    placeholder_profile: list[str] = []  # what the fixed dep MERGE leaves
    assert choke(placeholder_profile, _scope(world, "acme")) is False, "fail-closed to scoped"
    assert choke(placeholder_profile, _scope(world, None)) is True, "admin sees it"


def test_choke_matches_server_predicate_string(world):
    """Pin: the production `_scope_pred` still uses size()>0 + all() (no any()).

    If this fails, the choke was relaxed and the `tests/_scope_world.choke`
    model (used by this file and `test_profile_ancestor_scope_unit.py`) is
    stale - re-verify isolation against the new predicate before trusting them.
    """
    pred = world.srv._scope_pred("m")
    assert "$own IS NULL" in pred
    assert "size(m.profile) > 0" in pred
    assert "all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)" in pred
    assert "any(" not in pred, "choke must NOT use any() — that re-opens the collision leak"


# ---------------------------------------------------------------------------
# 4. F2 regression — `_owning_profiles` must RAISE on a falsy owner, never stamp
#    `['']`. `['']` is truthy so `if not _profiles_arr` would miss it and the
#    all() choke would deny that node to EVERY scoped tenant (a silent
#    fail-closed black hole). The canonical trigger is `Path('/').name == ''`.
# ---------------------------------------------------------------------------

def test_owning_profiles_raises_on_empty_owner_root_path():
    """`Path('/').name == ''` + no profile_name -> all candidates falsy -> raise."""
    from pathlib import Path

    repo_root_name = Path("/").name  # == ""
    assert repo_root_name == ""
    with pytest.raises(ValueError, match="owning profile"):
        _owning_profiles({"id": 99}, profile_name=None, repo_root_name=repo_root_name)


def test_owning_profiles_raises_on_all_empty_strings():
    """Explicit empty strings on every candidate also raise (no `['']` stamp)."""
    with pytest.raises(ValueError, match="owning profile"):
        _owning_profiles(
            {"id": 99, "profile_name": ""}, profile_name="", repo_root_name="",
        )


def test_owning_profiles_never_returns_falsy_element():
    """Property: every successful return is a single non-empty string (no `['']`)."""
    out = _owning_profiles({"id": 1}, profile_name="odoo_17", repo_root_name="")
    assert out == ["odoo_17"]
    assert out[0]  # truthy, real name


# ---------------------------------------------------------------------------
# 5. F4 regression — single source of truth for the owning profile. `_index_repo`
#    must feed the SAME owner to BOTH the Neo4j writer (`profiles=`) and the
#    pgvector write (`profile_name=`), so the two stores cannot diverge. Pure-unit:
#    a real on-disk module repo (no DB), a recording fake writer, a patched
#    `write_module_embeddings`, and a patched vector-extension probe. We assert the
#    captured pgvector `profile_name` equals the captured Neo4j `profiles[0]`.
# ---------------------------------------------------------------------------


class _CapturingWriter:
    """Fake IndexWriter that records the `profiles=` it is handed."""

    def __init__(self):
        self.profiles_seen: list[list[str]] = []

    def _rec(self, profiles=None, **_kw):
        if profiles is not None:
            self.profiles_seen.append(list(profiles))

    # All write_* methods the _index_repo write block calls take profiles=...
    def write_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    def write_view_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    def write_lint_violations(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    def write_js_graph_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    # WI-D: _index_repo calls write_asset_results (before write_view_results).
    def write_asset_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    # WI-1/WI-3: _index_repo now also calls these test-surface writers. Stubs
    # record the profiles= so the provenance contract is exercised here too.
    def write_test_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    def write_js_test_results(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    def write_stylesheets(self, *_a, profiles=None, **_kw):
        self._rec(profiles=profiles)

    # ADR-0056 B6: the --gc shim calls these instead of the removed gc_stale_modules.
    def server_now(self):
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def orphan_module_names(self, *_a, **_kw):
        return []

    def retire_modules(self, *_a, **_kw):
        return {"modules": 0, "children": 0, "by_label": {}, "retired": [],
                "skipped_recent": []}

    def reconcile_same_name_inherits(self, *_a, **_kw):
        # Post-pass added by #273 - called once per version from index_profile,
        # not from _index_repo.  Stub kept so _CapturingWriter satisfies the
        # IndexWriterProtocol contract (reconcile_same_name_inherits is declared there).
        return 0

    def relink_unresolved_placeholders(self, *_a, **_kw):
        return {}

    def gc_unresolved_placeholders(self, *_a, **_kw):
        return {}

    def gc_null_repo_dep_stubs(self, *_a, **_kw):
        return 0


def _seed_unit_module(repo, name="unitmod"):
    """Minimal Odoo module on disk (no DB) for _index_repo to parse."""
    import textwrap

    from tests.conftest import make_manifest

    module = repo / name
    make_manifest(module, name=name, version="99.0.1.0.0", depends=[])
    (module / "models").mkdir()
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())


def test_index_repo_feeds_same_owner_to_neo4j_and_pgvector(tmp_path, monkeypatch):
    """★ F4: Neo4j `profiles[0]` and pgvector `profile_name` come from ONE owner.

    Drives a real on-disk repo through `_index_repo` with a fake writer + a patched
    `write_module_embeddings`. The owner is computed ONCE (`_owning_profiles`); both
    stores must receive it. A future split-brain (e.g. pgvector stamping the run
    profile while Neo4j stamps the repo's own owner) would fail this assertion.
    """
    from tests.conftest import make_git_repo

    repo_dir = make_git_repo(tmp_path / "repo", "main")
    _seed_unit_module(repo_dir)
    import subprocess

    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-m", "seed"],
                   check=True, capture_output=True)

    captured_pg: dict[str, object] = {}

    def _fake_write_module_embeddings(mod_name, version, chunks, embedder,
                                      profile_name=None, **_kw):
        captured_pg["profile_name"] = profile_name
        return 0

    # pg_conn + embedder must be non-None to enter the embed branch; the vector
    # probe is patched True so we never touch a real Postgres.
    monkeypatch.setattr("src.db.migrate._vector_extension_available", lambda _c: True)
    monkeypatch.setattr(
        "src.indexer.writer_pgvector.write_module_embeddings",
        _fake_write_module_embeddings,
    )
    # The post-write head_sha persistence (last statement) touches Postgres; the
    # captures we assert on are all complete before it, so stub repo_store() out
    # to keep this a pure-unit test (no DB pool).
    from unittest.mock import MagicMock
    monkeypatch.setattr("src.indexer.pipeline.repo_store",
                        lambda: MagicMock())
    # With a pg_conn, _index_repo also observes the lifecycle ledger through the
    # process-wide pool (ADR-0056). Stub it like repo_store: otherwise a pool left
    # initialized by an earlier PG test (tables already dropped) makes this pure
    # unit test fail with UndefinedTable, but only in a combined run.
    monkeypatch.setattr("src.indexer.pipeline._presence_store", lambda: None)
    # Without a ledger the entity prune (B14) keeps the pre-B14 replace: it
    # deletes the owner's rows this parse did not produce, through the pool.
    # A fake pool + delete capture that owner too, so the pgvector delete is
    # held to the same F4 invariant (and no real pool left by an earlier PG
    # test is reached).
    import contextlib

    class _FakePool:
        @contextlib.contextmanager
        def checkout(self):
            yield object()

    def _fake_delete_except(conn, module, version, profile_name, keep_keys, **_kw):
        captured_pg["delete_profile_name"] = profile_name
        return 0

    monkeypatch.setattr("src.db.pg.get_pool", lambda: _FakePool())
    monkeypatch.setattr(
        "src.indexer.writer_pgvector.delete_module_embeddings_except",
        _fake_delete_except,
    )

    from src.indexer.pipeline import _index_repo

    writer = _CapturingWriter()
    repo = {
        "id": 1,
        "local_path": str(repo_dir),
        "odoo_version": "99.0",
        "url": "git@example.com:t/repo.git",
        # no profile_name column -> owner resolves to the run profile_name
    }
    _index_repo(
        repo, writer, pg_conn=object(), embedder=object(),
        full_reindex=True, profile_name="odoo_99",
    )

    # Neo4j side: every write_* got the SAME single owning profile.
    assert writer.profiles_seen, "expected at least one Neo4j write with profiles="
    assert all(p == ["odoo_99"] for p in writer.profiles_seen), writer.profiles_seen

    # pgvector side: stamped the SAME owner, NOT a separately-derived value.
    assert captured_pg.get("profile_name") == "odoo_99"
    assert captured_pg.get("delete_profile_name") == "odoo_99"
    # The load-bearing invariant: both stores agree by construction.
    assert captured_pg["profile_name"] == writer.profiles_seen[0][0]
