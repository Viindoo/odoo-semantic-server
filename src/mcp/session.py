# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-(API-key, MCP-session) sticky session state for implicit MCP context.

Implements Wave E (M11) implicit-context design from ADR-0029, amended by #251:
- ``get_session_state`` / ``set_active_version_db`` / ``set_active_profile_db`` —
  read and write the **in-memory** session-pin store.
- ``normalize_version_arg`` / ``normalize_profile_arg`` — collapse sentinel /
  empty strings to ``None``.
- ``resolve_version_v2`` — resolution order: explicit → session pin → latest fallback.
- ``resolve_profile_v2`` — resolution order: explicit → session pin → None.

Storage (#251):
- The in-memory store is the **source of truth** for the pin (NOT a cache of
  Postgres). One entry per ``(api_key_id, mcp_session_id)`` pair, so concurrent
  Claude Code sessions on one API key never clobber each other's version/profile.
- A session pin is ephemeral by nature: it **resets on server restart** (the MCP
  transport's ``mcp-session-id`` lives in-process and dies with it). Clients
  re-pin via ``set_active_*`` or pass an explicit version. This is intentional —
  the vestigial ``api_key_session_state`` table is no longer read or written.
- The store is size-bounded (``MCP_SESSION_PIN_MAX``, default 50000) with
  oldest-by-``set_at`` eviction so thousands of sessions cannot grow it
  unboundedly.
- Thread-safe via a single ``threading.Lock``; all critical sections are O(1)
  (dict get/put + at-most-one eviction, no I/O under the lock).
- Clock-injectable (``now_fn``) for deterministic unit testing.
- 24h TTL (since last ``set_active_*``) enforced at read time **in memory**:
  entries whose ``set_at`` is older than 24 hours are treated as ``None``
  (expired). The window is write-anchored, not access-sliding (reads do not
  refresh ``set_at``) — matching the pre-#251 DB behaviour.

See ``docs/adr/0029-implicit-session-context.md`` for design rationale.
"""

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SENTINELS: frozenset[str] = frozenset({"auto", "default", "latest", "version", "any", ""})

# Default mcp-session-id used by stdio / single-session / context-propagation-gap
# callers. The leading underscore guarantees no collision with a real
# ``uuid4().hex`` mcp-session-id; this bucket reproduces the pre-#251
# single-pin-per-key semantics byte-for-byte.
_NO_SESSION_SENTINEL: str = "_nosession"

_CACHE_TTL_SEC: float = 60.0
_SESSION_TTL_HOURS: int = 24

# Size bound for the in-memory pin store (A-SCALE-2). Oldest-by-``set_at``
# entry is evicted when the cap is exceeded. Env-tunable for peak concurrency.
_PIN_MAX: int = int(os.getenv("MCP_SESSION_PIN_MAX", "50000"))


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """Snapshot of one API key's active session context.

    Attributes:
        api_key_id: The API key that owns this state.
        odoo_version: Active Odoo version (e.g. ``"17.0"``).  ``None`` means
            not yet set — callers should fall back to ``_latest_version()``.
        profile_name: Active profile name.  ``None`` means not yet set.
    """

    api_key_id: str
    odoo_version: str | None
    profile_name: str | None


# ---------------------------------------------------------------------------
# In-memory pin store (module-level singleton, source of truth — #251)
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """One live session pin keyed by ``(api_key_id, mcp_session_id)``.

    Attributes:
        state: The live pin (``odoo_version`` + ``profile_name``). Never ``None``
            for a stored entry — absence of a pin is absence of an entry.
        set_at: Monotonic timestamp of the last write; drives both LRU eviction
            (A-SCALE-2) and the 24h TTL (write-anchored, since last set).
    """

    state: SessionState
    set_at: float


# OrderedDict so eviction (A-SCALE-2) and recency tracking are O(1): a write
# appends/moves its key to the end, so the oldest live pin is always at the
# front and ``popitem(last=False)`` evicts it in O(1) — keeping every
# ``_cache_lock`` critical section O(1) (A-SCALE-3) even at the size cap, so a
# rare overflow on the write path never stalls the shared-lock hot-path reads.
_cache: "OrderedDict[str, _CacheEntry]" = OrderedDict()
_cache_lock = threading.Lock()


def _ck(api_key_id: str, mcp_session_id: str) -> str:
    """Composite cache key for ``(api_key_id, mcp_session_id)``.

    Uses the ASCII unit-separator (``\\x1f``) so neither component can collide
    with the other regardless of content.
    """
    return f"{api_key_id}\x1f{mcp_session_id}"


def _evict_if_over_cap_locked() -> None:
    """Evict the oldest live pin(s) while the store exceeds ``_PIN_MAX``.

    Caller MUST hold ``_cache_lock``. O(1): the store is an ``OrderedDict`` kept
    in write-recency order (each write moves its key to the end), so the oldest
    pin is at the front and ``popitem(last=False)`` removes it in O(1). The
    common path is a single ``len`` comparison.
    """
    while len(_cache) > _PIN_MAX:
        _cache.popitem(last=False)


def _cache_get(api_key_id: str, mcp_session_id: str) -> _CacheEntry | None:
    """Return the live entry for the composite key, or ``None`` if absent.

    TTL is NOT applied here — :func:`get_session_state` owns idle-TTL semantics
    so the clock can be injected for deterministic tests.
    """
    with _cache_lock:
        return _cache.get(_ck(api_key_id, mcp_session_id))


def _cache_invalidate(api_key_id: str, mcp_session_id: str = _NO_SESSION_SENTINEL) -> None:
    """Drop the pin for one ``(api_key_id, mcp_session_id)`` pair."""
    with _cache_lock:
        _cache.pop(_ck(api_key_id, mcp_session_id), None)


# --- WI-3: tenant -> (own, shared) profile scope cache (60s, ADR-0034) ------
# Keyed by tenant_id. The admin/global (None tenant_id) case short-circuits to
# (None, []) = unrestricted; it is not cached.
_scope_cache: dict[int, tuple[tuple[list[str], list[str]], float]] = {}
_scope_lock = threading.Lock()


def invalidate_allowed_profiles(tenant_id: int | None = None) -> None:
    """Drop the cached tenant scope for *tenant_id* (or all when None)."""
    with _scope_lock:
        if tenant_id is None:
            _scope_cache.clear()
        else:
            _scope_cache.pop(tenant_id, None)


def resolve_tenant_scope(
    tenant_id: int | None,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> tuple[list[str] | None, list[str]]:
    """Return ``(own, shared)`` profile scope for a tenant (WI-3, ADR-0034), cached 60s.

    - ``own=None`` → admin / legacy global key (``tenant_id`` is None): UNRESTRICTED
      (the Neo4j choke point applies no filter; audited per ADR-0034 WI-7).
    - ``own=[...]`` → the tenant's directly-owned profiles (NOT the shared ancestors).
    - ``shared`` → all globally-shared profiles (``tenant_id IS NULL``), visible to all.

    The Neo4j array filter is ``any(node.profile ∩ own) OR all(node.profile ⊆ shared)``.
    """
    if tenant_id is None:
        return None, []
    now = now_fn()
    with _scope_lock:
        entry = _scope_cache.get(tenant_id)
        if entry is not None and entry[1] > now:
            own, shared = entry[0]
            return list(own), list(shared)
    from src.db.pg import repo_store  # lazy import — avoids circular dependency
    own, shared = repo_store().resolve_tenant_scope(tenant_id)
    with _scope_lock:
        _scope_cache[tenant_id] = ((own, shared), now + _CACHE_TTL_SEC)
    return list(own), list(shared)


def resolve_allowed_profiles(
    tenant_id: int | None,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> list[str] | None:
    """Flat union ``own ∪ shared`` for SINGLE-VALUE filters (pgvector ``profile_name``,
    profile-name listing). ``None`` = admin/unrestricted. ``[]`` = deny-all.
    The array-aware Neo4j choke point uses :func:`resolve_tenant_scope` instead.
    """
    own, shared = resolve_tenant_scope(tenant_id, now_fn=now_fn)
    if own is None:
        return None
    return sorted(set(own) | set(shared))


# ---------------------------------------------------------------------------
# Public API — sentinel normalization
# ---------------------------------------------------------------------------


def normalize_version_arg(version: str | None) -> str | None:
    """Collapse LLM-hallucinated sentinel strings to ``None``.

    The 6 sentinels are: ``"auto"``, ``"default"``, ``"latest"``,
    ``"version"``, ``"any"``, and the empty string ``""``.  Comparison
    is case-insensitive and strips surrounding whitespace.

    Args:
        version: Version string from an MCP tool call argument.

    Returns:
        ``None`` if *version* is ``None`` or a sentinel; otherwise the
        original string unchanged.

    Examples::

        >>> normalize_version_arg("17.0")
        '17.0'
        >>> normalize_version_arg("default") is None
        True
        >>> normalize_version_arg("") is None
        True
        >>> normalize_version_arg(None) is None
        True
    """
    if version is None:
        return None
    if version.strip().lower() in _SENTINELS:
        return None
    return version


def normalize_profile_arg(profile: str | None) -> str | None:
    """Collapse ``None`` / empty / whitespace-only profile names to ``None``.

    Profiles are not version sentinels, so no sentinel-word collapsing applies —
    this only normalizes "no profile given" forms to a single ``None``. The
    original (un-stripped) value is preserved when non-empty, since profile names
    are looked up verbatim.

    Args:
        profile: Profile name from an MCP tool call argument.

    Returns:
        ``None`` if *profile* is ``None`` or contains only whitespace; otherwise
        the original string unchanged.

    Examples::

        >>> normalize_profile_arg("my-erp-prod")
        'my-erp-prod'
        >>> normalize_profile_arg("") is None
        True
        >>> normalize_profile_arg("   ") is None
        True
        >>> normalize_profile_arg(None) is None
        True
    """
    if profile is None:
        return None
    if not profile.strip():
        return None
    return profile


# ---------------------------------------------------------------------------
# Public API — in-memory pin helpers
# ---------------------------------------------------------------------------


def get_session_state(
    api_key_id: str,
    mcp_session_id: str = _NO_SESSION_SENTINEL,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> SessionState | None:
    """Return the live pin for ``(api_key_id, mcp_session_id)``, or ``None``.

    Reads the in-memory store (source of truth — #251); performs **no DB I/O**.
    The 24h TTL (since last ``set_active_*``) is applied in memory: an entry
    whose ``set_at`` is older than ``_SESSION_TTL_HOURS`` is treated as expired
    (returns ``None`` and the stale entry is evicted). Write-anchored, not
    access-sliding — reads do not refresh ``set_at``.

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
        mcp_session_id: The MCP transport session id; defaults to the
            single-session sentinel for stdio / context-propagation-gap callers.
        now_fn: Monotonic clock callable.  Override in tests for deterministic
            TTL testing.

    Returns:
        :class:`SessionState` or ``None`` if no live pin exists.
    """
    # Non-numeric api_key_id (e.g. 'default' sentinel in tests / stdio) → no
    # authenticated key in scope → no session (#248 / #251 contract).
    try:
        int(api_key_id)
    except (ValueError, TypeError):
        return None

    cutoff = _SESSION_TTL_HOURS * 3600
    now = now_fn()
    key = _ck(api_key_id, mcp_session_id)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        # 24h TTL (since last set_active_*) — re-read set_at UNDER the lock so a
        # concurrent writer that just refreshed this entry is never clobbered:
        # only evict when it is STILL stale at lock-acquire time (avoids the
        # read-then-evict TOCTOU that could delete a freshly-set pin).
        if now - entry.set_at > cutoff:
            _cache.pop(key, None)
            return None
        # Return a snapshot, never the live entry.state: writers mutate that
        # object in place under this same lock, so handing the caller the live
        # reference would let it observe a half-updated (version/profile) view.
        return replace(entry.state)


def set_active_version_db(
    api_key_id: str,
    odoo_version: str,
    mcp_session_id: str = _NO_SESSION_SENTINEL,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Pin *odoo_version* for ``(api_key_id, mcp_session_id)`` in memory.

    Sets ONLY ``odoo_version`` (and refreshes ``set_at`` to reset the 24h idle
    TTL), preserving any ``profile_name`` already pinned in the same entry — so
    setting version then profile (or vice-versa) never clobbers the other field.
    Performs **no DB I/O** (#251). The function name and ``bool`` return are kept
    for backward compat (``server.py`` + tests depend on them).

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
            A non-numeric value (``'default'`` sentinel, CLI, stdio, or a
            context-propagation gap) skips the write — no error.
        odoo_version: A concrete version string such as ``"17.0"``.
        mcp_session_id: The MCP transport session id; defaults to the
            single-session sentinel.
        now_fn: Monotonic clock callable (override in tests).

    Returns:
        ``True`` when stored; ``False`` when skipped because *api_key_id* was
        non-numeric.  Callers use this to avoid emitting a "set" receipt for a
        write that never happened (#248).
    """
    # #248 loud-fail guard: a lost/non-numeric key id must not silently succeed.
    try:
        int(api_key_id)
    except (ValueError, TypeError):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "set_active_version_db: non-numeric api_key_id %r — skipping store "
            "(authenticated HTTP should never reach this; see #248)",
            api_key_id,
        )
        return False

    now = now_fn()
    key = _ck(api_key_id, mcp_session_id)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            _cache[key] = _CacheEntry(
                state=SessionState(
                    api_key_id=api_key_id,
                    odoo_version=odoo_version,
                    profile_name=None,
                ),
                set_at=now,
            )
        else:
            entry.state.odoo_version = odoo_version
            entry.set_at = now
            _cache.move_to_end(key)  # refresh write-recency for O(1) LRU eviction
        _evict_if_over_cap_locked()
    return True


def set_active_profile_db(
    api_key_id: str,
    profile_name: str | None,
    mcp_session_id: str = _NO_SESSION_SENTINEL,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Pin *profile_name* for ``(api_key_id, mcp_session_id)`` in memory.

    Sets ONLY ``profile_name`` (``None`` clears it) and refreshes ``set_at``,
    preserving any ``odoo_version`` already pinned in the same entry. Mirrors
    :func:`set_active_version_db`: no DB I/O, same ``bool`` return + #248 guard.

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
            A non-numeric value skips the write — no error.
        profile_name: Profile name such as ``"my-erp-prod"``, or ``None`` to
            clear the active profile.
        mcp_session_id: The MCP transport session id; defaults to the
            single-session sentinel.
        now_fn: Monotonic clock callable (override in tests).

    Returns:
        ``True`` when stored; ``False`` when skipped because *api_key_id* was
        non-numeric (mirrors ``set_active_version_db`` — #248).
    """
    # #248 loud-fail guard.
    try:
        int(api_key_id)
    except (ValueError, TypeError):
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "set_active_profile_db: non-numeric api_key_id %r — skipping store "
            "(authenticated HTTP should never reach this; see #248)",
            api_key_id,
        )
        return False

    now = now_fn()
    key = _ck(api_key_id, mcp_session_id)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            if profile_name is None:
                # Clearing a profile on a session with no pin is a no-op — do
                # not create an empty (None, None) entry that would waste a cap
                # slot and could evict a real pin under MCP_SESSION_PIN_MAX.
                return True
            _cache[key] = _CacheEntry(
                state=SessionState(
                    api_key_id=api_key_id,
                    odoo_version=None,
                    profile_name=profile_name,
                ),
                set_at=now,
            )
        else:
            entry.state.profile_name = profile_name
            entry.set_at = now
            _cache.move_to_end(key)  # refresh write-recency for O(1) LRU eviction
        _evict_if_over_cap_locked()
    return True


# ---------------------------------------------------------------------------
# Public API — version resolution
# ---------------------------------------------------------------------------


def resolve_version_v2(
    version_arg: str | None,
    api_key_id: str,
    session,  # neo4j session — used only for fallback
    mcp_session_id: str = _NO_SESSION_SENTINEL,
) -> str:
    """Resolve a version argument using the 3-tier order defined by ADR-0029.

    Resolution order:
    1. **Explicit** — *version_arg* after sentinel normalization.
    2. **Session pin** — ``get_session_state(api_key_id, mcp_session_id).odoo_version``
       (in-memory, per-session — #251).
    3. **Latest fallback** — ``_latest_version(session)`` from the Neo4j index.

    Args:
        version_arg: Raw version argument from the MCP tool call (may be
            a sentinel string, ``None``, or a concrete version).
        api_key_id: The API key that owns the session state.
        session: An open Neo4j driver session used as the last-resort
            fallback to discover the latest indexed version.
        mcp_session_id: The MCP transport session id, scoping the Tier-2 pin
            to one live session; defaults to the single-session sentinel.

    Returns:
        A concrete Odoo version string (e.g. ``"17.0"``).

    Raises:
        ValueError: If all three tiers fail to produce a version (empty index
            + no session + no explicit version).
    """
    # Tier 1: explicit version provided by the caller
    explicit = normalize_version_arg(version_arg)
    if explicit is not None:
        return explicit

    # Tier 2: session pin — prefer stored version if present
    state = get_session_state(api_key_id, mcp_session_id)
    if state is not None and state.odoo_version:
        return state.odoo_version

    # Tier 3: latest-version fallback via Neo4j index.
    # Import _latest_version (not _resolve_version) to avoid infinite recursion:
    # _resolve_version now delegates back to resolve_version_v2, so calling it
    # here would loop.  _latest_version is a pure Neo4j query with no recursion.
    from src.mcp.server import _latest_version  # noqa: PLC0415
    v = _latest_version(session)
    if v is None:
        raise ValueError(
            "No data indexed. Run `python -m src.indexer index-repo --profile <name>` first."
        )
    return v


def resolve_profile_v2(
    profile_arg: str | None,
    api_key_id: str,
    session,  # unused — kept for signature parity with resolve_version_v2
    mcp_session_id: str = _NO_SESSION_SENTINEL,
) -> str | None:
    """Resolve a profile argument to a *proposed default* (#251). No authz.

    Resolution order:
    1. **Explicit** — *profile_arg* after :func:`normalize_profile_arg`; returned
       verbatim if non-empty.
    2. **Session pin** — ``get_session_state(api_key_id, mcp_session_id).profile_name``
       (in-memory, per-session) if set.
    3. **None** — no pin → defer to the caller's existing default behaviour.

    This proposes which profile to *default to* when the caller omits one; it
    performs NO authorization. The server-side ADR-0034 choke re-validates the
    proposed profile at read time (narrowing-only, fail-closed) — that authz
    lives in WI-2, not here.

    Args:
        profile_arg: Raw profile argument from the MCP tool call (may be empty,
            ``None``, or a concrete profile name).
        api_key_id: The API key that owns the session state.
        session: Unused; present only for signature parity with
            :func:`resolve_version_v2`.
        mcp_session_id: The MCP transport session id, scoping the Tier-2 pin
            to one live session; defaults to the single-session sentinel.

    Returns:
        A profile name, or ``None`` when neither an explicit arg nor a pin
        supplies one.
    """
    # Tier 1: explicit profile provided by the caller.
    explicit = normalize_profile_arg(profile_arg)
    if explicit is not None:
        return explicit

    # Tier 2: session pin.
    state = get_session_state(api_key_id, mcp_session_id)
    if state is not None and state.profile_name:
        return state.profile_name

    # Tier 3: no pin → caller's default applies.
    return None
