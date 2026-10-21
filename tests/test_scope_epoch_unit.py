# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP process drops its profile-scope caches when ANOTHER process changes a profile.

Business rule (ADR-0034 T3): the Web UI moves a profile to
another tenant in its own process; ``invalidate_allowed_profiles`` there cannot
reach the MCP server. The MCP side polls the registry's scope fingerprint at
most every ``_SCOPE_EPOCH_CHECK_SEC`` and, when it changed, drops the tenant
scope, the shared list and the ancestor chains; the resource cache key carries
the epoch so a body rendered under the old scope is never served again. The
old tenant therefore loses access within seconds, not after the 60 s scope TTL
or the 300 s resource TTL.
"""
from __future__ import annotations

import pytest

from src.mcp import session


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Store:
    def __init__(self):
        self.fingerprint = "fp-1"
        self.scope = (["acme_17"], ["odoo_17"])
        self.scope_reads = 0
        self.fingerprint_down = False

    def scope_fingerprint(self):
        if self.fingerprint_down:
            raise ConnectionError("registry down")
        return self.fingerprint

    def resolve_tenant_scope(self, tenant_id):
        self.scope_reads += 1
        own, shared = self.scope
        return list(own), list(shared)

    def get_ancestor_profile_names(self, name):
        return [name, "odoo_17"]


@pytest.fixture
def world(monkeypatch):
    store = _Store()
    monkeypatch.setattr("src.db.pg.repo_store", lambda: store)
    monkeypatch.setattr(session, "_scope_epoch_value", None)
    monkeypatch.setattr(session, "_scope_epoch_checked_at", None)
    session.invalidate_allowed_profiles()
    yield store, _Clock()
    session.invalidate_allowed_profiles()


def test_a_tenant_move_in_another_process_is_seen_within_the_poll_interval(world):
    store, clock = world
    assert session.resolve_tenant_scope(7, now_fn=clock) == (["acme_17"], ["odoo_17"])
    # the Web UI (another process) moves acme_17 away from tenant 7
    store.fingerprint, store.scope = "fp-2", ([], ["odoo_17"])
    clock.t += session._SCOPE_EPOCH_CHECK_SEC + 0.1  # far below the 60 s scope TTL
    assert session.resolve_tenant_scope(7, now_fn=clock) == ([], ["odoo_17"])
    assert store.scope_reads == 2


def test_an_unchanged_registry_keeps_serving_the_cache(world):
    store, clock = world
    session.resolve_tenant_scope(7, now_fn=clock)
    clock.t += session._SCOPE_EPOCH_CHECK_SEC + 0.1
    session.resolve_tenant_scope(7, now_fn=clock)
    assert store.scope_reads == 1


def test_the_registry_is_polled_at_most_once_per_interval(world, monkeypatch):
    store, clock = world
    polls = []
    real = store.scope_fingerprint
    monkeypatch.setattr(store, "scope_fingerprint", lambda: polls.append(1) or real())
    for _ in range(50):
        session.resolve_tenant_scope(7, now_fn=clock)
        clock.t += 0.01
    assert len(polls) == 1


def test_fingerprint_outage_keeps_the_ttl_bounded_cache(world):
    store, clock = world
    session.resolve_tenant_scope(7, now_fn=clock)
    store.fingerprint_down = True
    clock.t += session._SCOPE_EPOCH_CHECK_SEC + 0.1
    assert session.resolve_tenant_scope(7, now_fn=clock) == (["acme_17"], ["odoo_17"])
    assert store.scope_reads == 1


def test_resource_cache_key_changes_with_the_scope_epoch(world, monkeypatch):
    from src.mcp import resources, server

    store, _clock = world
    monkeypatch.setattr(server, "_get_tenant_id", lambda: 7)
    monkeypatch.setattr(server, "_resolve_profile", lambda _p: None)
    before = resources._tenant_cache_key("17.0", "model", "sale.order")
    store.fingerprint = "fp-2"
    monkeypatch.setattr(session, "_scope_epoch_checked_at", None)  # poll interval elapsed
    after = resources._tenant_cache_key("17.0", "model", "sale.order")
    assert before != after
    assert before.startswith("odoo://17.0/model/sale.order::t7")
