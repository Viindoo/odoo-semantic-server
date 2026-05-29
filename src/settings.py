# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin Settings overlay resolver (ADR-0042).

3-tier resolution: L1 in-memory LRU (60s TTL, bounded 5000) -> L2 Postgres
app_settings -> L3 code default from SETTINGS_CATALOGUE.

Per ADR-0042: TIDAK block startup on DB failure; fallback to code default.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.db._types import PgConn
from src.settings_registry import SETTINGS_CATALOGUE

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 5000

# (key, tenant_id_or_None) -> (value, expiry_monotonic)
_cache: dict[tuple[str, int | None], tuple[Any, float]] = {}


def get_setting(key: str, *, tenant_id: int | None = None, conn: PgConn | None = None) -> Any:
    """Resolve setting through 3-tier chain. Cached 60s per worker.

    conn: optional psycopg2 connection. If None, checkout from the module-level pool.

    Cache key (WI-R F-009 doc):
        ``(key, tenant_id_or_None)`` — note that a direct admin/system call
        with ``tenant_id=None`` shares the same cache slot as an internal call
        that omits the kwarg.  This is INTENTIONAL: both paths resolve to the
        system row (or catalogue default), so a single slot is semantically
        correct and maximises cache hit-rate.  Do NOT "fix" this by adding a
        sentinel for the omitted-kwarg case — it would halve the hit rate.
    """
    cache_key = (key, tenant_id)
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expiry = cached
        if now < expiry:
            return value

    try:
        value = _resolve_from_db(key, tenant_id, conn)
    except Exception as exc:
        log.warning("Setting %s DB resolve failed; using default: %s", key, exc)
        value = None

    if value is None:
        value = _resolve_default(key)

    _evict_if_full()
    _cache[cache_key] = (value, now + _CACHE_TTL_SECONDS)
    return value


def get_setting_typed[T](
    key: str,
    expected_type: type[T],
    *,
    tenant_id: int | None = None,
    conn: PgConn | None = None,
) -> T:
    """Typed accessor — raises TypeError on mismatch."""
    value = get_setting(key, tenant_id=tenant_id, conn=conn)
    if not isinstance(value, expected_type):
        raise TypeError(
            f"Setting {key!r}: expected {expected_type.__name__},"
            f" got {type(value).__name__} ({value!r})"
        )
    return value  # type: ignore[return-value]


def invalidate_setting(key: str, *, tenant_id: int | None = None) -> None:
    """Called after PATCH to clear in-process cache. Other workers TTL-expire <=60s."""
    _cache.pop((key, tenant_id), None)


def invalidate_all() -> None:
    """Nuclear option — clear entire cache. Use sparingly (e.g. plan tier bulk update)."""
    _cache.clear()


def get_overlay_only(
    key: str,
    *,
    tenant_id: int | None = None,
    conn: PgConn | None = None,
) -> Any | None:
    """Return the DB-overlay row for *key*, or ``None`` if no row exists.

    Unlike :func:`get_setting`, this helper does NOT fall back to the
    ``SETTINGS_CATALOGUE`` default — a missing row is reported as
    ``None``.  It is the supported public alias for the previously
    "private" ``_query_settings`` call shape used by
    :mod:`src.indexer.embedder`, :mod:`src.git_utils`, and
    :mod:`src.mcp.resources` to honour their class-attribute or
    module-default fallbacks (WI-R F-005 — kept callers stable while
    decoupling them from the private symbol name).

    Skips the in-process cache because callers that need this shape
    specifically want a fresh DB read on every call (typically on a
    cold worker startup).
    """
    try:
        return _resolve_from_db(key, tenant_id, conn)
    except Exception as exc:
        log.warning("Setting %s overlay-only resolve failed: %s", key, exc)
        return None


def _resolve_from_db(key: str, tenant_id: int | None, conn: PgConn | None) -> Any:
    """Query app_settings; tenant override WINS system row."""
    if conn is not None:
        return _query_settings(key, tenant_id, conn)

    # No conn supplied — checkout from pool
    from src.db.pg import get_pool
    pool = get_pool()
    with pool.checkout() as pooled_conn:
        return _query_settings(key, tenant_id, pooled_conn)


def _query_settings(key: str, tenant_id: int | None, conn: PgConn) -> Any:
    """Execute the 2-query settings lookup against a specific connection."""
    with conn.cursor() as cur:
        # Tenant override first
        if tenant_id is not None:
            cur.execute(
                "SELECT value_json FROM app_settings"
                " WHERE key = %s AND scope = 'tenant' AND tenant_id = %s",
                (key, tenant_id),
            )
            row = cur.fetchone()
            if row is not None:
                return _unwrap(row[0])
        # System row
        cur.execute(
            "SELECT value_json FROM app_settings"
            " WHERE key = %s AND scope = 'system' AND tenant_id IS NULL",
            (key,),
        )
        row = cur.fetchone()
        return _unwrap(row[0]) if row else None


def _resolve_default(key: str) -> Any:
    for sdef in SETTINGS_CATALOGUE:
        if sdef.key == key:
            return sdef.default_value
    raise KeyError(f"Setting {key!r} not in SETTINGS_CATALOGUE")


def _unwrap(value_json: Any) -> Any:
    """app_settings.value_json stored as {"v": <actual>} to normalize JSONB primitives."""
    if isinstance(value_json, dict) and "v" in value_json:
        return value_json["v"]
    return value_json


def _evict_if_full() -> None:
    """Simple LRU-ish eviction: drop oldest 10% when cache exceeds max."""
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        n_to_drop = max(1, _CACHE_MAX_ENTRIES // 10)
        # Drop oldest by expiry (approximation of LRU; cheap)
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][1])[:n_to_drop]
        for k, _ in oldest:
            _cache.pop(k, None)
