# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared harness for the no-DB tenant-scope unit tests (ADR-0034, #385).

The REAL ``src.mcp.server._scope`` / ``src.mcp.session`` code runs against a
stubbed profile registry (:class:`ScopeRegistry`, the ``RepoStore`` read API the
session layer calls) and a stubbed caller (tenant id + session pin).

:func:`choke` is the one Python model of the Cypher choke built by
``server._scope_pred`` - a unit test cannot run Cypher. It is pinned to the
production predicate string by
``tests/test_scope_choke_shared_core_unit.py::test_choke_matches_server_predicate_string``;
the Neo4j-marked suites run the predicate itself.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field


def choke(node_profile: list[str], scope: dict) -> bool:
    """``($own IS NULL OR (size(n.profile) > 0 AND all(p IN n.profile WHERE
    p IN $own OR p IN $shared)))`` over one node's ``profile[]``."""
    own, shared = scope["own"], scope["shared"]
    if own is None:
        return True
    if not node_profile:
        return False
    allowed = set(own) | set(shared)
    return all(p in allowed for p in node_profile)


class ScopeRegistry:
    """Stub of the ``RepoStore`` read API used by ``src.mcp.session``."""

    def __init__(self, parents: dict[str, str | None], own: dict, shared: list[str]):
        self.parents = parents
        self.own = own
        self.shared = list(shared)
        self.chain_calls: list[str] = []
        self.down = False

    def get_ancestor_profile_names(self, name):
        self.chain_calls.append(name)
        if self.down:
            raise ConnectionError("registry unreachable")
        out = []
        while name is not None and name in self.parents:
            out.append(name)
            name = self.parents[name]
        return out

    def resolve_tenant_scope(self, tenant_id):
        return sorted(self.own[tenant_id]), list(self.shared)

    def shared_profile_names(self):
        return list(self.shared)


@dataclass
class ScopeWorld:
    """The modules under test (bound to the CURRENT ``sys.modules`` entries:
    other unit tests drop and re-import ``src.mcp.*``) and the stubbed caller."""

    srv: object
    session: object
    reg: ScopeRegistry
    state: dict = field(default_factory=lambda: {"tenant": None, "pin": None})
    degraded: list[str] = field(default_factory=list)

    def as_(self, tenant, pin=None) -> None:
        self.state["tenant"] = tenant
        self.state["pin"] = pin

    def scope(self, profile_name=None) -> dict:
        return self.srv._scope(profile_name)


def make_world(monkeypatch, *, parents, own, shared) -> ScopeWorld:
    """Install the stubs; the caller's fixture must call
    ``world.session.invalidate_allowed_profiles()`` on teardown."""
    srv = importlib.import_module("src.mcp.server")
    session = importlib.import_module("src.mcp.session")
    reg = ScopeRegistry(parents, own, shared)
    monkeypatch.setattr("src.db.pg.repo_store", lambda: reg)
    session.invalidate_allowed_profiles()
    world = ScopeWorld(srv=srv, session=session, reg=reg)
    monkeypatch.setattr(srv, "_get_tenant_id", lambda: world.state["tenant"])
    monkeypatch.setattr(srv, "_resolve_profile", lambda _arg: world.state["pin"])
    monkeypatch.setattr("src.mcp.degraded.mark_degraded", world.degraded.append)
    return world
