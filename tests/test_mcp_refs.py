# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/mcp/refs.py — per-API-key ref minter with TTL + tenant isolation.

Covers AC-C1-1 through AC-C1-6:
  C1-1: mint stability (same items + same api_key → same refs within TTL)
  C1-2: resolve_ref returns canonical for live ref; None-or-RefError on miss/expire
  C1-3: tenant isolation (API-A refs != API-B refs; no cross-leak)
  C1-4: thread-safety (50 concurrent threads, disjoint api_key_ids)
  C1-5: TTL enforcement via injected clock (at 4:59 → live; at 5:00 → RefError)
  C1-6: ≥8 tests total; exhaustion sentinel when >MAX_ITEMS_PER_CALL items
"""

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from src.mcp.refs import (
    MAX_ITEMS_PER_CALL,
    RefError,
    RefMinter,
    mint_refs,
    resolve_ref,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELD_ITEM = {"field_name": "amount_total", "model": "sale.order"}
_METHOD_ITEM = {"method_name": "action_confirm", "model": "sale.order"}
_VIEW_ITEM = {"xmlid": "sale.view_order_form", "model": "sale.order"}
_MODULE_ITEM = {"module_name": "sale", "depends": []}
_PATTERN_ITEM = {"pattern_name": "compute_field_pattern", "odoo_version": "17.0"}


def _make_minter(ttl: float = 300.0, now_fn=None) -> RefMinter:
    """Factory for an isolated RefMinter (not the global singleton)."""
    if now_fn is None:
        import time
        now_fn = time.monotonic
    return RefMinter(ttl=ttl, now_fn=now_fn)


# ---------------------------------------------------------------------------
# AC-C1-1: Mint stability
# ---------------------------------------------------------------------------


class TestMintStability:
    """AC-C1-1 — same items + same api_key within TTL → same refs."""

    def test_mint_returns_list_of_strings(self):
        """mint_refs returns a list of strings, one per item."""
        minter = _make_minter()
        items = [_FIELD_ITEM, _FIELD_ITEM.copy()]
        refs = minter.mint(items, api_key_id="key-A")
        assert isinstance(refs, list)
        assert len(refs) == 2
        assert all(isinstance(r, str) for r in refs)

    def test_mint_same_items_same_key_stable_within_ttl(self):
        """Minting the same list twice within TTL returns identical refs."""
        minter = _make_minter()
        items = [_FIELD_ITEM, _METHOD_ITEM]
        refs1 = minter.mint(items, api_key_id="key-stable")
        refs2 = minter.mint(items, api_key_id="key-stable")
        assert refs1 == refs2, (
            f"Expected stable refs on second call, got {refs1!r} then {refs2!r}"
        )

    def test_mint_different_key_gets_fresh_refs(self):
        """Different api_key_id gets independent fresh counters."""
        minter = _make_minter()
        item = _FIELD_ITEM
        refs_a = minter.mint([item], api_key_id="key-A")
        refs_b = minter.mint([item], api_key_id="key-B")
        # Both should be "f1" (fresh counter per tenant) — not cross-contaminated
        assert refs_a == ["f1"]
        assert refs_b == ["f1"]

    def test_mint_prefix_auto_inference(self):
        """kind='auto' infers correct prefix from item keys."""
        minter = _make_minter()
        field_refs = minter.mint([_FIELD_ITEM], api_key_id="key-prefix", kind="auto")
        method_refs = minter.mint([_METHOD_ITEM], api_key_id="key-prefix", kind="auto")
        view_refs = minter.mint([_VIEW_ITEM], api_key_id="key-prefix", kind="auto")
        assert field_refs[0].startswith("f"), f"Expected 'f' prefix, got {field_refs[0]}"
        assert method_refs[0].startswith("m"), f"Expected 'm' prefix, got {method_refs[0]}"
        assert view_refs[0].startswith("v"), f"Expected 'v' prefix, got {view_refs[0]}"

    def test_mint_ambiguous_item_raises_valueerror(self):
        """Items with no recognized keys raise ValueError when kind='auto'."""
        minter = _make_minter()
        with pytest.raises(ValueError, match="ambiguous"):
            minter.mint([{"unknown_key": "value"}], api_key_id="key-err")

    def test_mint_explicit_kind_overrides_auto(self):
        """Explicit kind= bypasses auto-inference."""
        minter = _make_minter()
        # _MODULE_ITEM doesn't have 'field_name' but explicit kind='module' should work
        refs = minter.mint([_MODULE_ITEM], api_key_id="key-explicit", kind="module")
        assert refs[0].startswith("x")


# ---------------------------------------------------------------------------
# AC-C1-2: resolve_ref behaviour
# ---------------------------------------------------------------------------


class TestResolveRef:
    """AC-C1-2 — resolve returns canonical for live ref; RefError on miss/expire."""

    def test_resolve_live_ref_returns_canonical(self):
        """resolve returns the exact canonical dict that was minted."""
        minter = _make_minter()
        item = {"field_name": "amount_total", "model": "sale.order", "ttype": "monetary"}
        (ref,) = minter.mint([item], api_key_id="key-resolve")
        result = minter.resolve(ref, api_key_id="key-resolve")
        assert result == item

    def test_resolve_unknown_ref_raises_referror(self):
        """resolve raises RefError for a ref that was never minted."""
        minter = _make_minter()
        # Seed any ref so the store exists for this key
        minter.mint([_FIELD_ITEM], api_key_id="key-unknown")
        with pytest.raises(RefError, match="not found or expired"):
            minter.resolve("f999", api_key_id="key-unknown")

    def test_resolve_unknown_key_raises_referror(self):
        """resolve raises RefError when the api_key_id has no store at all."""
        minter = _make_minter()
        with pytest.raises(RefError, match="not found"):
            minter.resolve("f1", api_key_id="nonexistent-key")

    def test_resolve_expired_ref_raises_referror_with_hint(self):
        """After TTL, resolve raises RefError with a recovery_hint."""
        tick = [0.0]

        def fake_clock() -> float:
            return tick[0]

        minter = _make_minter(ttl=300.0, now_fn=fake_clock)
        (ref,) = minter.mint([_FIELD_ITEM], api_key_id="key-expire")

        # Advance past TTL
        tick[0] = 301.0

        with pytest.raises(RefError) as exc_info:
            minter.resolve(ref, api_key_id="key-expire")

        err = exc_info.value
        assert err.recovery_hint, "RefError must carry a non-empty recovery_hint"
        assert "re-run" in err.recovery_hint.lower() or "list_" in err.recovery_hint.lower()


# ---------------------------------------------------------------------------
# AC-C1-3: Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """AC-C1-3 — API-key A refs are invisible to API-key B (no cross-leak)."""

    def test_tenant_isolation_no_cross_leak(self):
        """api_key A's 'f1' → canonical X; api_key B's 'f1' → canonical Y; no leak."""
        minter = _make_minter()

        item_a = {"field_name": "amount_total", "model": "sale.order"}
        item_b = {"field_name": "price_unit", "model": "sale.order.line"}

        (ref_a,) = minter.mint([item_a], api_key_id="tenant-A")
        (ref_b,) = minter.mint([item_b], api_key_id="tenant-B")

        # Both counters start at 1 — refs are both "f1" but different canonicals
        assert ref_a == "f1"
        assert ref_b == "f1"

        resolved_a = minter.resolve(ref_a, api_key_id="tenant-A")
        resolved_b = minter.resolve(ref_b, api_key_id="tenant-B")

        assert resolved_a == item_a
        assert resolved_b == item_b
        assert resolved_a != resolved_b

    def test_tenant_a_ref_invisible_to_tenant_b(self):
        """Resolving A's ref from B's namespace raises RefError."""
        minter = _make_minter()

        item_a = {"field_name": "amount_total", "model": "sale.order"}
        minter.mint([item_a], api_key_id="tenant-X")

        # "f1" was minted under tenant-X, but we try to resolve under tenant-Y
        with pytest.raises(RefError):
            minter.resolve("f1", api_key_id="tenant-Y")


# ---------------------------------------------------------------------------
# AC-C1-4: Thread-safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """AC-C1-4 — 50 concurrent threads with disjoint api_key_ids; no exceptions."""

    def test_concurrent_mint_and_resolve_50_threads(self):
        """50 threads each mint + resolve with a unique api_key_id — no failures."""
        minter = _make_minter()
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(thread_idx: int) -> None:
            api_key = f"thread-key-{thread_idx}"
            item = {"field_name": f"field_{thread_idx}", "model": "sale.order"}
            try:
                refs = minter.mint([item], api_key_id=api_key)
                assert len(refs) == 1
                canonical = minter.resolve(refs[0], api_key_id=api_key)
                assert canonical == item
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(worker, i) for i in range(50)]
            for f in as_completed(futures):
                f.result()  # re-raise any thread exception

        assert not errors, f"Thread errors: {errors}"


# ---------------------------------------------------------------------------
# AC-C1-5: TTL enforcement with injected clock
# ---------------------------------------------------------------------------


class TestTTLClock:
    """AC-C1-5 — clock-injected TTL: 4:59 → live; 5:00+ → RefError."""

    def test_ref_alive_at_4m59s(self):
        """Ref is still resolvable 1 second before expiry."""
        tick = [0.0]

        def fake_clock() -> float:
            return tick[0]

        minter = _make_minter(ttl=300.0, now_fn=fake_clock)
        (ref,) = minter.mint([_FIELD_ITEM], api_key_id="key-ttl")

        # 1 second before TTL expires
        tick[0] = 299.0
        result = minter.resolve(ref, api_key_id="key-ttl")
        assert result == _FIELD_ITEM

    def test_ref_expired_at_5m00s(self):
        """Ref raises RefError exactly at TTL boundary (t=300)."""
        tick = [0.0]

        def fake_clock() -> float:
            return tick[0]

        minter = _make_minter(ttl=300.0, now_fn=fake_clock)
        (ref,) = minter.mint([_FIELD_ITEM], api_key_id="key-ttl2")

        # Exactly at TTL boundary
        tick[0] = 300.0
        with pytest.raises(RefError):
            minter.resolve(ref, api_key_id="key-ttl2")

    def test_ref_expired_well_past_ttl(self):
        """Ref raises RefError well past expiry."""
        tick = [0.0]

        def fake_clock() -> float:
            return tick[0]

        minter = _make_minter(ttl=300.0, now_fn=fake_clock)
        (ref,) = minter.mint([_FIELD_ITEM], api_key_id="key-ttl3")

        tick[0] = 9999.0
        with pytest.raises(RefError) as exc_info:
            minter.resolve(ref, api_key_id="key-ttl3")
        # Verify recovery_hint is present and non-empty
        assert exc_info.value.recovery_hint


# ---------------------------------------------------------------------------
# AC-C1-6: Exhaustion sentinel + module-level functions smoke test
# ---------------------------------------------------------------------------


class TestExhaustionSentinel:
    """AC-C1-6 — items beyond MAX_ITEMS_PER_CALL get sentinel 'exhausted'."""

    def test_exhaustion_sentinel_beyond_cap(self):
        """Items beyond MAX_ITEMS_PER_CALL (1000) return 'exhausted'."""
        minter = _make_minter()
        # Build 1002 items: all valid field dicts with distinct field_name
        items = [
            {"field_name": f"field_{i}", "model": "sale.order"}
            for i in range(MAX_ITEMS_PER_CALL + 2)
        ]
        refs = minter.mint(items, api_key_id="key-exhaust")

        assert len(refs) == len(items)
        # First MAX_ITEMS_PER_CALL refs should be normal (start with "f")
        for i in range(MAX_ITEMS_PER_CALL):
            cap = MAX_ITEMS_PER_CALL
            assert refs[i] != "exhausted", f"Item {i} should not be exhausted (cap={cap})"
        # Items beyond cap should be sentinels
        for i in range(MAX_ITEMS_PER_CALL, len(items)):
            assert refs[i] == "exhausted", f"Item {i} should be 'exhausted'"

    def test_exactly_at_cap_no_sentinel(self):
        """Exactly MAX_ITEMS_PER_CALL items — no sentinel."""
        minter = _make_minter()
        items = [{"field_name": f"f_{i}", "model": "sale.order"} for i in range(MAX_ITEMS_PER_CALL)]
        refs = minter.mint(items, api_key_id="key-exact-cap")
        assert "exhausted" not in refs
        assert len(refs) == MAX_ITEMS_PER_CALL


class TestModuleLevelFunctions:
    """Smoke test for module-level mint_refs / resolve_ref convenience functions."""

    def test_module_level_round_trip(self):
        """mint_refs + resolve_ref via module-level singleton (different key per test)."""
        item = {"field_name": "amount_total", "model": "sale.order"}
        # Use a unique key to avoid collision with other tests' global singleton state
        api_key = "module-level-test-key-unique-1"
        refs = mint_refs([item], api_key_id=api_key)
        assert len(refs) == 1
        canonical = resolve_ref(refs[0], api_key_id=api_key)
        assert canonical == item
