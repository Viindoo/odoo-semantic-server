# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-API-key opaque ref minter — Pattern 5 (Playwright e7/f1e3 style).

Each call to ``mint_refs`` assigns compact stable IDs like ``f1``, ``m3``,
``v7`` to a list of canonical dicts returned by Cypher.  A subsequent
``resolve_ref`` call recovers the original dict using the ref + the same
api_key_id.

Design principles:
- **Tenant-isolated**: refs are scoped to (api_key_id, ref_id).  API key A's
  ``f1`` and API key B's ``f1`` are independent namespaces.
- **5-minute TTL**: stale refs raise :class:`RefError` with a recovery hint.
  The hint names the list_* tool to re-run, matching Playwright's
  "Ref not found — try re-capturing snapshot" pattern.
- **Thread-safe**: a single ``threading.Lock`` guards all mutations.
- **Lazy eviction**: expired entries are swept on every mint/resolve call.
  No background thread is needed; refs are cheap enough.
- **Stable within TTL**: minting the same items list with the same api_key_id
  returns the same ref IDs as the previous call (content-hash lookup).
- **Cap at 1000 items per call**: requests beyond 1000 items per
  ``mint_refs`` call emit a sentinel string ``"exhausted"`` for each
  overflow item.  This prevents unbounded memory growth if a caller passes
  a very large result set.  The caller should paginate instead.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_TTL_SECONDS: float = 300.0  # 5 minutes — matches Playwright convention
MAX_ITEMS_PER_CALL: int = 1000  # items beyond this index get sentinel "exhausted"

PREFIX_BY_KIND: dict[str, str] = {
    "field": "f",
    "method": "m",
    "view": "v",
    "pattern": "p",
    "module": "x",
}

# Keys present in item dicts that reliably identify the kind.
_KIND_DETECTOR: list[tuple[str, str]] = [
    ("field_name", "field"),
    ("method_name", "method"),
    ("xmlid", "view"),
    ("pattern_name", "pattern"),
    ("module_name", "module"),
]


class RefError(Exception):
    """Raised when a ref is stale (existed but expired) or structurally invalid.

    Attributes:
        recovery_hint: Human-readable instruction to recover — e.g.
            "Ref 'f12' expired — re-run the list_* call that minted it."
    """

    def __init__(self, message: str, *, recovery_hint: str = "") -> None:
        super().__init__(message)
        self.recovery_hint = recovery_hint


@dataclass
class _Entry:
    canonical: dict
    expires_at: float


@dataclass
class _TenantStore:
    """Per-api_key storage: live refs + per-prefix counters + content-hash index."""

    refs: dict[str, _Entry] = field(default_factory=dict)
    # Per-prefix next counter: {"f": 3, "m": 1, ...}
    next_id: dict[str, int] = field(default_factory=dict)
    # content-hash → ref_id, for stable re-minting within TTL
    hash_to_ref: dict[int, str] = field(default_factory=dict)


def _infer_kind(item: dict) -> str:
    """Infer the kind string from an item dict's keys.

    Raises:
        ValueError: if the item shape is ambiguous and ``kind='auto'`` was passed.
    """
    for key, kind in _KIND_DETECTOR:
        if key in item:
            return kind
    raise ValueError(
        "kind='auto' but item shape is ambiguous — cannot infer kind from keys "
        f"{set(item.keys())!r}. Pass kind= explicitly "
        "(one of: 'field', 'method', 'view', 'pattern', 'module')."
    )


def _canonical_hash(item: dict) -> int:
    """Stable hash of a canonical dict for idempotent re-minting."""
    try:
        return hash(tuple(sorted((k, str(v)) for k, v in item.items())))
    except Exception:
        # Unhashable values fall back to id-based identity (no stability guarantee).
        return id(item)


class RefMinter:
    """Thread-safe per-api_key ref store with lazy TTL eviction.

    Args:
        ttl: Lifetime in seconds for each minted ref.  Default 300 (5 min).
        now_fn: Callable returning the current monotonic time.  Injectable
            for deterministic testing — monkeypatch this in unit tests.
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._now = now_fn
        self._lock = threading.Lock()
        # Nested map: api_key_id → _TenantStore
        self._stores: dict[str, _TenantStore] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mint(
        self,
        items: list[dict],
        api_key_id: str,
        kind: str = "auto",
    ) -> list[str]:
        """Assign short ref IDs to *items* and return them in the same order.

        Items beyond :data:`MAX_ITEMS_PER_CALL` receive the sentinel string
        ``"exhausted"`` — callers should paginate rather than passing huge lists.

        Args:
            items: List of canonical dicts (Cypher result rows).
            api_key_id: Tenant identifier; refs are scoped to this key.
            kind: Prefix selector — one of ``PREFIX_BY_KIND`` keys or ``"auto"``.
                In ``"auto"`` mode the kind is inferred from each item's keys.
                All items in a single call must have the same kind when
                ``kind='auto'``; raise :class:`ValueError` otherwise.

        Returns:
            List of ref IDs (e.g. ``["f1", "f2", "f3"]``) in the same order as
            *items*.  Refs for identical items within TTL are stable.
        """
        now = self._now()
        result: list[str] = []

        with self._lock:
            store = self._get_or_create_store(api_key_id)
            self._evict_expired(store, now)

            for idx, item in enumerate(items):
                if idx >= MAX_ITEMS_PER_CALL:
                    result.append("exhausted")
                    continue

                # Determine prefix
                effective_kind = _infer_kind(item) if kind == "auto" else kind
                prefix = PREFIX_BY_KIND.get(effective_kind)
                if prefix is None:
                    raise ValueError(
                        f"Unknown kind {effective_kind!r}. "
                        f"Valid kinds: {list(PREFIX_BY_KIND)}"
                    )

                # Stable re-minting: reuse existing ref if hash matches + not expired
                ch = _canonical_hash(item)
                existing_ref = store.hash_to_ref.get(ch)
                if existing_ref is not None and existing_ref in store.refs:
                    entry = store.refs[existing_ref]
                    if entry.expires_at > now:
                        # Refresh TTL on reuse so the ref stays alive
                        entry.expires_at = now + self._ttl
                        result.append(existing_ref)
                        continue
                    else:
                        # Expired — remove stale hash mapping
                        del store.hash_to_ref[ch]

                # Mint a new ref
                counter = store.next_id.get(prefix, 1)
                ref_id = f"{prefix}{counter}"
                store.next_id[prefix] = counter + 1

                store.refs[ref_id] = _Entry(
                    canonical=item,
                    expires_at=now + self._ttl,
                )
                store.hash_to_ref[ch] = ref_id
                result.append(ref_id)

        return result

    def resolve(self, ref: str, api_key_id: str) -> dict:
        """Return the canonical dict for *ref* scoped to *api_key_id*.

        Args:
            ref: Opaque ref ID such as ``"f12"``.
            api_key_id: Must match the api_key_id used when minting.

        Returns:
            The original canonical dict.

        Raises:
            RefError: If the ref existed but has expired (TTL), or if the ref
                was never minted for this api_key_id.
        """
        now = self._now()

        with self._lock:
            store = self._stores.get(api_key_id)
            if store is None:
                raise RefError(
                    f"Ref {ref!r} not found — no refs minted for this API key.",
                    recovery_hint=(
                        f"Ref {ref!r} not found — re-run the list_* call "
                        "that minted it."
                    ),
                )

            self._evict_expired(store, now)

            entry = store.refs.get(ref)
            if entry is None:
                # Determine if this looks like it might have expired vs. never existed.
                # We can't tell for certain after eviction, so use a generic message.
                raise RefError(
                    f"Ref {ref!r} not found or expired.",
                    recovery_hint=(
                        f"Ref {ref!r} expired or was never minted for this session — "
                        "re-run the list_* call that minted it to get fresh refs."
                    ),
                )

            return entry.canonical

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create_store(self, api_key_id: str) -> _TenantStore:
        """Return the _TenantStore for api_key_id, creating it if absent.

        Must be called under self._lock.
        """
        if api_key_id not in self._stores:
            self._stores[api_key_id] = _TenantStore()
        return self._stores[api_key_id]

    def _evict_expired(self, store: _TenantStore, now: float) -> None:
        """Remove all expired entries from *store*.

        Must be called under self._lock.
        """
        expired_refs = [
            ref_id
            for ref_id, entry in store.refs.items()
            if entry.expires_at <= now
        ]
        for ref_id in expired_refs:
            del store.refs[ref_id]

        # Clean up hash_to_ref mappings that pointed to expired refs
        stale_hashes = [
            ch
            for ch, ref_id in store.hash_to_ref.items()
            if ref_id not in store.refs
        ]
        for ch in stale_hashes:
            del store.hash_to_ref[ch]


# ---------------------------------------------------------------------------
# Module-level singleton + convenience functions
# ---------------------------------------------------------------------------

_GLOBAL_MINTER: RefMinter = RefMinter()


def mint_refs(
    items: list[dict],
    api_key_id: str,
    kind: str = "auto",
) -> list[str]:
    """Mint opaque ref IDs for *items*, scoped to *api_key_id*.

    Delegates to the module-level :class:`RefMinter` singleton.

    Returns:
        List of ref IDs in the same order as *items*.
        Items beyond :data:`MAX_ITEMS_PER_CALL` return ``"exhausted"``.
    """
    return _GLOBAL_MINTER.mint(items, api_key_id, kind=kind)


def resolve_ref(ref: str, api_key_id: str) -> dict:
    """Recover the canonical dict for *ref* scoped to *api_key_id*.

    Delegates to the module-level :class:`RefMinter` singleton.

    Raises:
        RefError: Stale or unknown ref.
    """
    return _GLOBAL_MINTER.resolve(ref, api_key_id)
