# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_profile_ancestor_scope_unit.py
"""#385 - a profile_name filter resolves the profile's ACCESSIBLE ancestor chain.

Pure-unit (no Neo4j, no Postgres): the real ``server._scope`` /
``server._effective_allowed`` / ``session.resolve_profile_chain`` /
``lifecycle_read.profile_filter_lines`` run against a stubbed profile registry
and tenant boundary (``tests/_scope_world.py``). A node's visibility is decided
by evaluating the REAL scope params with the harness's Python model of the
ADR-0034 ``all()`` choke (pinned to the production predicate string in
``tests/test_scope_choke_shared_core_unit.py``).

Business rules under protection:

- under ``profile_name=child`` a module owned only by the child's PARENT is
  visible when the caller already owns that parent (the #385 bug);
- the filter never widens: a sibling, a descendant, or another tenant's
  private ancestor stays denied - a parent link alone grants nothing;
- an out-of-scope profile denies everything and its chain is never looked up;
- profile registry down -> the profile alone (no widening), marked degraded;
- the pgvector / RLS list equals the Neo4j own ∪ shared set (lockstep);
- "not indexed" answers name only in-scope profiles; an inaccessible ancestor
  is a COUNT, never a name (no cross-tenant existence oracle).
"""
from __future__ import annotations

import pytest

from tests._scope_world import choke as _choke
from tests._scope_world import make_world

# Bound by the ``world`` fixture to the CURRENT modules: other unit tests drop
# and re-import ``src.mcp.*`` from sys.modules, so an import-time reference
# could patch a stale module object that the code under test no longer uses.
srv = None
session = None

# ---------------------------------------------------------------------------
# World: odoo_17 (shared) <- standard_viindoo_17 (viindoo) <- viindoo_internal_17
# (viindoo); viindoo_other_17 is a viindoo SIBLING of internal (same parent);
# viindoo_internal_17_child is a viindoo DESCENDANT of internal;
# acme_17 (acme) has parent globex_private_17 (globex) - a foreign ancestor.
# ---------------------------------------------------------------------------
PARENTS = {
    "odoo_17": None,
    "standard_viindoo_17": "odoo_17",
    "viindoo_internal_17": "standard_viindoo_17",
    "viindoo_other_17": "standard_viindoo_17",
    "viindoo_internal_17_child": "viindoo_internal_17",
    "globex_private_17": "odoo_17",
    "acme_17": "globex_private_17",
}
OWN = {
    1: ["standard_viindoo_17", "viindoo_internal_17", "viindoo_internal_17_child",
        "viindoo_other_17"],
    2: ["acme_17"],
    3: ["globex_private_17"],
}
SHARED = ["odoo_17"]
VIINDOO, ACME = 1, 2


@pytest.fixture
def world(monkeypatch):
    global srv, session
    w = make_world(monkeypatch, parents=PARENTS, own=OWN, shared=SHARED)
    srv, session = w.srv, w.session
    yield {"reg": w.reg, "state": w.state, "degraded": w.degraded}
    session.invalidate_allowed_profiles()


def _as(world, tenant, pin=None):
    world["state"]["tenant"] = tenant
    world["state"]["pin"] = pin


# ---------------------------------------------------------------------------
# Neo4j scope (server._scope)
# ---------------------------------------------------------------------------

def test_child_filter_returns_a_module_owned_only_by_the_callers_private_parent(world):
    """The #385 bug: tvtmaaddons modules (profile=[standard_viindoo_17]) must be
    visible under profile_name=viindoo_internal_17 for a key that owns both."""
    _as(world, VIINDOO)
    scope = srv._scope("viindoo_internal_17")
    assert _choke(["standard_viindoo_17"], scope) is True
    assert _choke(["viindoo_internal_17"], scope) is True
    assert _choke(["odoo_17"], scope) is True


def test_child_filter_still_denies_sibling_and_descendant_profiles(world):
    """The chain goes UP only: a sibling under the same parent and a child of
    the filtered profile are not part of it (no over-widening)."""
    _as(world, VIINDOO)
    scope = srv._scope("viindoo_internal_17")
    assert _choke(["viindoo_other_17"], scope) is False
    assert _choke(["viindoo_internal_17_child"], scope) is False


def test_foreign_tenant_ancestor_is_never_granted_by_the_parent_link(world):
    """acme_17's parent belongs to globex: acme must not read it through the chain."""
    _as(world, ACME)
    scope = srv._scope("acme_17")
    assert "globex_private_17" not in scope["own"] + scope["shared"]
    assert _choke(["globex_private_17"], scope) is False
    assert _choke(["acme_17"], scope) is True
    assert _choke(["odoo_17"], scope) is True


def test_out_of_scope_profile_denies_all_and_is_not_looked_up(world):
    """acme naming viindoo's profile: deny-all, and its chain is never read
    (the chain length would otherwise be an existence oracle)."""
    _as(world, ACME)
    assert srv._scope("viindoo_internal_17") == {"own": [], "shared": []}
    assert srv._effective_allowed("viindoo_internal_17") == []
    assert world["reg"].chain_calls == []


def test_registry_down_narrows_to_the_profile_alone_and_marks_degraded(world):
    world["reg"].down = True
    _as(world, VIINDOO)
    scope = srv._scope("viindoo_internal_17")
    assert scope["own"] == ["viindoo_internal_17"]
    assert _choke(["standard_viindoo_17"], scope) is False, "never widened"
    assert world["degraded"], "a registry failure must mark the body degraded"


def test_admin_narrowing_covers_the_whole_chain(world):
    _as(world, None)
    scope = srv._scope("acme_17")
    assert scope["own"] == ["acme_17", "globex_private_17", "odoo_17"]
    assert _choke(["globex_private_17"], scope) is True
    assert _choke(["viindoo_internal_17"], scope) is False, "still a narrowing"


def test_session_pin_resolves_the_same_chain(world):
    _as(world, VIINDOO, pin="viindoo_internal_17")
    assert _choke(["standard_viindoo_17"], srv._scope(None)) is True
    assert _choke(["viindoo_other_17"], srv._scope(None)) is False


def test_chain_cache_is_dropped_by_invalidate_allowed_profiles(world):
    """A re-parent invalidates the cache (web UI calls invalidate_allowed_profiles)."""
    _as(world, VIINDOO)
    srv._scope("viindoo_internal_17")
    srv._scope("viindoo_internal_17")
    assert world["reg"].chain_calls == ["viindoo_internal_17"], "cached 60s"
    session.invalidate_allowed_profiles()
    srv._scope("viindoo_internal_17")
    assert world["reg"].chain_calls == ["viindoo_internal_17"] * 2


# ---------------------------------------------------------------------------
# pgvector / RLS list (server._effective_allowed) in lockstep with Neo4j
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant,profile", [
    (VIINDOO, "viindoo_internal_17"),
    (VIINDOO, "standard_viindoo_17"),
    (VIINDOO, "odoo_17"),
    (ACME, "acme_17"),
    (ACME, "viindoo_internal_17"),
    (None, "viindoo_internal_17"),
    (VIINDOO, None),
])
def test_pgvector_list_equals_the_neo4j_own_and_shared_set(world, tenant, profile):
    _as(world, tenant)
    scope = srv._scope(profile)
    allowed = srv._effective_allowed(profile)
    if scope["own"] is None:
        assert allowed is None
    else:
        assert set(allowed) == set(scope["own"]) | set(scope["shared"])
        assert srv._allowed_to_guc(allowed).split(",") == allowed or allowed == []


def test_pgvector_list_carries_the_parent_and_the_profile_first(world):
    _as(world, VIINDOO)
    allowed = srv._effective_allowed("viindoo_internal_17")
    assert allowed[0] == "viindoo_internal_17"
    assert "standard_viindoo_17" in allowed
    assert "viindoo_other_17" not in allowed


# ---------------------------------------------------------------------------
# "not indexed" wording (lifecycle_read.profile_filter_lines)
# ---------------------------------------------------------------------------

class _FakeNeo:
    """Returns the module's profile array for the boundary query, else None."""

    def __init__(self, module_profiles):
        self.module_profiles = module_profiles
        self.scopes: list[dict] = []

    def single(self, _session, _cypher, _label, **params):
        self.scopes.append({"own": params.get("own"), "shared": params.get("shared")})
        if self.module_profiles is None or not _choke(self.module_profiles, params):
            return None
        return {"profiles": list(self.module_profiles)}


def _lines(world, monkeypatch, *, tenant, profile, module_profiles):
    from src.mcp.lifecycle_read import profile_filter_lines

    _as(world, tenant)
    neo = _FakeNeo(module_profiles)
    monkeypatch.setattr(srv, "_single_bounded", neo.single)
    return profile_filter_lines(srv, None, "x_mod", "17.0", profile)


def test_miss_names_an_in_scope_profile_outside_the_chain(world, monkeypatch):
    out = _lines(world, monkeypatch, tenant=VIINDOO, profile="viindoo_internal_17",
                 module_profiles=["viindoo_other_17"])
    assert out == (
        "├─ Profile filter:  indexed in viindoo_other_17 (in your scope), outside"
        " profile viindoo_internal_17 and its ancestor profiles",
    )


def test_miss_counts_an_inaccessible_ancestor_without_naming_it(world, monkeypatch):
    out = _lines(world, monkeypatch, tenant=ACME, profile="acme_17",
                 module_profiles=["globex_private_17"])
    assert out == (
        "├─ Profile filter:  1 ancestor profile of acme_17 not visible to this key;"
        " results exclude them",
    )
    assert "globex" not in "".join(out)


def test_miss_says_nothing_for_an_out_of_scope_profile_or_without_a_filter(
        world, monkeypatch):
    assert _lines(world, monkeypatch, tenant=ACME, profile="viindoo_internal_17",
                  module_profiles=["viindoo_internal_17"]) == ()
    assert _lines(world, monkeypatch, tenant=ACME, profile=None,
                  module_profiles=["acme_17"]) == ()


# ---------------------------------------------------------------------------
# Resource cache key: a pinned render never serves an unpinned session
# ---------------------------------------------------------------------------

def test_resource_cache_key_separates_pinned_sessions(world):
    from src.mcp.resources import _tenant_cache_key

    _as(world, VIINDOO)
    unpinned = _tenant_cache_key("17.0", "module", "sale")
    _as(world, VIINDOO, pin="viindoo_internal_17")
    pinned = _tenant_cache_key("17.0", "module", "sale")
    assert unpinned == "odoo://17.0/module/sale::t1"
    assert pinned != unpinned and pinned.endswith("::pviindoo_internal_17")
