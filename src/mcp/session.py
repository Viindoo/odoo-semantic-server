# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-API-key sticky session state for implicit MCP context (Pattern 6).

Implements Wave E (M11) implicit-context design from ADR-0029:
- ``get_session_state`` / ``set_active_version_db`` / ``set_active_profile_db`` —
  read and write the ``api_key_session_state`` table.
- ``normalize_version_arg`` — collapses 6 sentinel strings to ``None``.
- ``resolve_version_v2`` — resolution order: explicit → session DB → latest fallback.

Cache:
- 60-second in-memory cache keyed by ``api_key_id``.
- Thread-safe via a single ``threading.Lock``.
- Clock-injectable (``now_fn``) for deterministic unit testing.
- 24h sliding TTL enforced at read time: rows older than 24 hours are treated
  as None (expired), not as stale data.

See ``migrations/0005_api_key_session_state.sql`` for the DB schema.
See ``docs/adr/0029-implicit-session-context.md`` for design rationale.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SENTINELS: frozenset[str] = frozenset({"auto", "default", "latest", "version", "any", ""})
_CACHE_TTL_SEC: float = 60.0
_SESSION_TTL_HOURS: int = 24


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
# In-memory cache (module-level singleton)
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    state: SessionState | None
    # Monotonic clock expiry (seconds) — not wall-clock
    expires_at: float


_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def _cache_get(api_key_id: str, now: float) -> tuple[bool, SessionState | None]:
    """Return ``(hit, state)``.  hit=True means cache is valid (may be None = expired row)."""
    with _cache_lock:
        entry = _cache.get(api_key_id)
        if entry is None or entry.expires_at <= now:
            return False, None
        return True, entry.state


def _cache_set(
    api_key_id: str,
    state: SessionState | None,
    now: float,
    ttl: float = _CACHE_TTL_SEC,
) -> None:
    with _cache_lock:
        _cache[api_key_id] = _CacheEntry(state=state, expires_at=now + ttl)


def _cache_invalidate(api_key_id: str) -> None:
    with _cache_lock:
        _cache.pop(api_key_id, None)


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


# ---------------------------------------------------------------------------
# Public API — DB helpers
# ---------------------------------------------------------------------------


def get_session_state(
    api_key_id: str,
    *,
    now_fn: Callable[[], float] = time.monotonic,
) -> SessionState | None:
    """Return the current session state for *api_key_id*, or ``None``.

    Resolution:
    1. 60-second in-memory cache hit → return immediately.
    2. SELECT from ``api_key_session_state`` with 24h TTL filter.
    3. Cache result (including ``None``) for 60 seconds.

    24h TTL is enforced via SQL: ``updated_at > NOW() - INTERVAL '24 hours'``.
    A stale (>24h) or absent row both return ``None``.

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
        now_fn: Monotonic clock callable.  Override in tests for deterministic
            TTL testing.

    Returns:
        :class:`SessionState` or ``None`` if no live session exists.
    """
    now = now_fn()

    # 1. Cache hit
    hit, cached_state = _cache_get(api_key_id, now)
    if hit:
        return cached_state

    # 2. DB lookup
    state = _fetch_from_db(api_key_id)

    # 3. Populate cache
    _cache_set(api_key_id, state, now)
    return state


def _fetch_from_db(api_key_id: str) -> SessionState | None:
    """Execute the DB lookup.  Returns ``None`` if row absent or >24h old."""
    # Non-numeric api_key_id (e.g. 'default' sentinel in tests / stdio) → no session.
    try:
        key_int = int(api_key_id)
    except (ValueError, TypeError):
        return None

    from src.mcp.server import _checkout_pg  # lazy import avoids circular dependency

    sql = """
        SELECT odoo_version, profile_name
        FROM api_key_session_state
        WHERE api_key_id = %s
          AND updated_at > NOW() - INTERVAL '24 hours'
    """
    try:
        with _checkout_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (key_int,))
                row = cur.fetchone()
    except Exception:
        # Pool not initialised (test context / cold start) → treat as no session.
        return None

    if row is None:
        return None
    odoo_version, profile_name = row
    return SessionState(
        api_key_id=api_key_id,
        odoo_version=odoo_version or None,
        profile_name=profile_name or None,
    )


def set_active_version_db(api_key_id: str, odoo_version: str) -> None:
    """Persist *odoo_version* as the active version for *api_key_id*.

    Performs an UPSERT into ``api_key_session_state``, updating ``updated_at``
    to reset the 24h sliding TTL.  Invalidates the 60-second in-memory cache
    for *api_key_id* so the next ``get_session_state`` call reads fresh data.

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
            If *api_key_id* is the sentinel ``'default'`` or any other
            non-numeric string (e.g. tests, CLI, stdio transport), the persist
            is silently skipped — no DB write, no error.
        odoo_version: A concrete version string such as ``"17.0"``.
    """
    # Belt-and-suspenders guard: the 'default' sentinel means no authenticated
    # key is in scope (unit tests, CLI, stdio transport, or a context-propagation
    # gap).  Silently skip rather than crash with ValueError: invalid literal.
    try:
        key_int = int(api_key_id)
    except (ValueError, TypeError):
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "set_active_version_db: non-numeric api_key_id %r — skipping persist",
            api_key_id,
        )
        return

    sql = """
        INSERT INTO api_key_session_state (api_key_id, odoo_version, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (api_key_id) DO UPDATE
            SET odoo_version = EXCLUDED.odoo_version,
                updated_at   = NOW()
    """
    from src.mcp.server import _checkout_pg

    with _checkout_pg() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (key_int, odoo_version))

    _cache_invalidate(api_key_id)


def set_active_profile_db(api_key_id: str, profile_name: str | None) -> None:
    """Persist *profile_name* as the active profile for *api_key_id*.

    Performs an UPSERT into ``api_key_session_state``, updating ``updated_at``
    to reset the 24h sliding TTL.  Invalidates the 60-second in-memory cache
    for *api_key_id* so the next ``get_session_state`` call reads fresh data.

    Args:
        api_key_id: The API key identifier (string form of the integer PK).
            If *api_key_id* is the sentinel ``'default'`` or any other
            non-numeric string, the persist is silently skipped.
        profile_name: Profile name such as ``"my-erp-prod"``, or ``None``
            to clear the active profile.
    """
    # Belt-and-suspenders guard: skip persist for non-numeric api_key_id.
    try:
        key_int = int(api_key_id)
    except (ValueError, TypeError):
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "set_active_profile_db: non-numeric api_key_id %r — skipping persist",
            api_key_id,
        )
        return

    sql = """
        INSERT INTO api_key_session_state (api_key_id, profile_name, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (api_key_id) DO UPDATE
            SET profile_name = EXCLUDED.profile_name,
                updated_at   = NOW()
    """
    from src.mcp.server import _checkout_pg

    with _checkout_pg() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (key_int, profile_name))

    _cache_invalidate(api_key_id)


# ---------------------------------------------------------------------------
# Public API — version resolution
# ---------------------------------------------------------------------------


def resolve_version_v2(
    version_arg: str | None,
    api_key_id: str,
    session,  # neo4j session — used only for fallback
) -> str:
    """Resolve a version argument using the 3-tier order defined by ADR-0029.

    Resolution order:
    1. **Explicit** — *version_arg* after sentinel normalization.
    2. **Session DB** — ``get_session_state(api_key_id).odoo_version``.
    3. **Latest fallback** — ``_latest_version(session)`` from the Neo4j index.

    Args:
        version_arg: Raw version argument from the MCP tool call (may be
            a sentinel string, ``None``, or a concrete version).
        api_key_id: The API key that owns the session state.
        session: An open Neo4j driver session used as the last-resort
            fallback to discover the latest indexed version.

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

    # Tier 2: session DB — prefer stored version if present
    state = get_session_state(api_key_id)
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
