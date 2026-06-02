# SPDX-License-Identifier: AGPL-3.0-or-later
"""API key authentication middleware for MCP server."""
import asyncio
import json as _json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import psycopg2
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.auth import hash_key as _hash_key
from src.constants import DEFAULT_RATE_LIMIT_RPM, PG_BG_RETRY_INTERVAL_SECONDS
from src.db.exceptions import PoolNotInitializedError


@dataclass(frozen=True)
class PlanInfo:
    """Immutable plan metadata snapshot cached per api_key_id.

    Per-key override fields (M10B P0-ext, ADR-0041):
      rate_limit_override: when not None, overrides plan.rate_limit_rpm.
                           Value 0 = zero-allowed (NOT unlimited — see D5).
      quota_override:      when not None, overrides plan.quota_calls_per_month.
                           Same 0 = zero-allowed semantics.
    Unlimited ONLY via slug='unlimited' (ADR-0041 D5 SSOT).
    """

    plan_id: int
    slug: str
    quota_calls_per_month: int  # 0 = unlimited sentinel when slug='unlimited'
    rate_limit_rpm: int
    rate_limit_override: int | None = None  # NULL in DB = use plan default
    quota_override: int | None = None       # NULL in DB = use plan default


def _resolve_effective_rpm(plan: "PlanInfo") -> tuple[int, bool]:
    """Resolve effective RPM limit from plan slug + per-key override.

    Returns (effective_rpm, is_unlimited).

    Resolution order per ADR-0041 D5:
      1. slug='unlimited' → (0, True)  # bypass — slug is the SSOT for unlimited
      2. rate_limit_override is not None → (override, False)  # explicit value;
         override=0 means zero-allowed, NOT unlimited.
      3. plan.rate_limit_rpm → (rpm, False)  # plan default

    This keeps the 'unlimited' slug as the single SSOT for bypass and ensures
    admin-set override=0 (explicit zero-allowed) is never silently promoted to
    unlimited behavior.
    """
    if plan.slug == "unlimited":
        return 0, True
    if plan.rate_limit_override is not None:
        return plan.rate_limit_override, False
    return plan.rate_limit_rpm, False


def _resolve_effective_quota(plan: "PlanInfo") -> tuple[int, bool]:
    """Resolve effective monthly quota from plan slug + per-key override.

    Returns (effective_quota, is_unlimited).

    Same resolution order as _resolve_effective_rpm — see that docstring.
    """
    if plan.slug == "unlimited":
        return 0, True
    if plan.quota_override is not None:
        return plan.quota_override, False
    return plan.quota_calls_per_month, False


def _degraded_response() -> Response:
    """Build the canonical 503 response when the DB tier is unavailable.

    Incident 2026-05-19 root cause: a single OperationalError during auth
    propagated to a 500, which gave callers no way to distinguish "your
    request was bad" from "the service is in degraded mode". A 503 with a
    machine-readable JSON body lets clients/proxies retry intelligently.

    Body is STATIC — no exception payload echoed to the unauthenticated
    caller. `psycopg2.OperationalError.__str__` from libpq routinely
    includes internal hostnames, private IPs, DB usernames, and database
    names (everything except the password, which libpq strips). Exposing
    that on a public endpoint during a DB outage is CWE-209 info
    disclosure for any service that is internet-facing. Diagnostics live
    in the server-side log only (see callers of this helper).
    """
    body = _json.dumps({
        "status": "degraded",
        "pg": "unavailable",
    })
    return Response(
        body,
        status_code=503,
        media_type="application/json",
        # Retry-After is HTTP-string per RFC 7231 §7.1.3; derive from the
        # integer SSOT so it stays in lockstep with the background retry
        # cadence in src/mcp/server.py._bg_retry_init_pool.
        headers={"Retry-After": str(PG_BG_RETRY_INTERVAL_SECONDS)},
    )

_logger = logging.getLogger(__name__)

# In-memory cache: hash(raw_key) -> (api_key_id | None, timestamp)
# Keys stored as SHA-256 hashes — never plaintext in RAM (I2).
_KEY_CACHE: dict[str, int | None] = {}
_CACHE_TS: dict[str, float] = {}
_CACHE_TTL = 300.0  # 5 minutes
_cache_lock = threading.Lock()  # Protects read/write to _KEY_CACHE and _CACHE_TS

# Secondary cache for tenant_id — same TTL and hash key as _KEY_CACHE.
# Stored separately to preserve the existing _KEY_CACHE / _cache_set signature
# used by tests and cache-invalidation helpers.
# Value is tenant_id (int) for tenant-bound keys, or None for global/admin keys.
_TENANT_CACHE: dict[str, int | None] = {}

# Owner-metadata cache for the read-side authorization guard (defense-in-depth,
# ADR-0034 follow-up). Same TTL and hash key as _KEY_CACHE — populated in the
# SAME _do_verify round-trip via verify_api_key_full, so the guard costs no extra
# DB query per cache window. Value is (user_id | None, owner_is_admin: bool):
#   - user_id None    → system/CLI key (no webui_users owner)  → unrestricted OK
#   - owner_is_admin  → admin-owned key                         → unrestricted OK
# Absence of an entry (TTL hit but no value) means the verify path that warmed
# the cache predates this field; _cache_get_owner returns (False, ...) for that
# case so the middleware re-verifies rather than fail-open (mirrors _TENANT_CACHE
# split fail-closed handling).
_OWNER_CACHE: dict[str, tuple[int | None, bool]] = {}

# Plan cache: api_key_id -> (PlanInfo, monotonic_timestamp)
# Shares _CACHE_TTL and _cache_lock with _KEY_CACHE to avoid additional lock.
_PLAN_CACHE: dict[int, tuple[PlanInfo, float]] = {}

# Usage buffer: api_key_id -> pending increments not yet flushed to DB.
# Flushed to usage_counter via background task when buffer hits _USAGE_FLUSH_THRESHOLD.
_usage_buffer: dict[int, int] = {}
_usage_buffer_lock = threading.Lock()
_USAGE_FLUSH_THRESHOLD = 10  # flush after this many buffered increments across all keys

# ---------------------------------------------------------------------------
# Per-API-key sliding-window rate limiter (WI-8)
# ---------------------------------------------------------------------------
# Maps api_key_id → deque of monotonic timestamps for requests in the last 60s.
_rate_buckets: dict[int, deque] = {}
_rate_lock = threading.Lock()


def _check_rate_limit(api_key_id: int, plan_info: "PlanInfo") -> tuple[bool, int]:
    """Sliding-window rate limiter. Returns (allowed, remaining).

    Thread-safe: guarded by _rate_lock.

    Args:
        api_key_id: The authenticated API key id.
        plan_info:  PlanInfo for the key — provides rate_limit_rpm and
                    optional rate_limit_override.

    Returns:
        (True, remaining)  if the request is within the limit.
        (False, 0)         if the window is exhausted.

    Unlimited path: plan slug='unlimited' bypasses the bucket entirely
    (ADR-0041 D5). Override=0 is NOT unlimited — it means zero-allowed.
    """
    effective_rpm, is_unlimited = _resolve_effective_rpm(plan_info)
    if is_unlimited:
        # slug='unlimited' sentinel — bypass rate limit gate entirely.
        return True, 0

    now = time.monotonic()
    window = 60.0
    with _rate_lock:
        bucket = _rate_buckets.setdefault(api_key_id, deque())
        # Prune timestamps older than the window
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        remaining = max(0, effective_rpm - len(bucket))
        if len(bucket) >= effective_rpm:
            return False, 0
        bucket.append(now)
        return True, remaining - 1


def _get_plan_for_key(api_key_id: int, pg_pool) -> "PlanInfo":
    """Return PlanInfo for api_key_id, consulting _PLAN_CACHE first.

    Cache miss triggers a DB query joining api_keys + plans.
    Uses _cache_lock (same lock as _KEY_CACHE) to avoid adding a new lock.
    TTL: _CACHE_TTL (300 s).

    Args:
        api_key_id: The authenticated API key id.
        pg_pool:    psycopg2 connection pool.

    Returns:
        PlanInfo for the key.

    Raises:
        KeyError / psycopg2.Error if the row is unexpectedly absent.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _PLAN_CACHE.get(api_key_id)
        if cached is not None:
            plan_info, ts = cached
            if now - ts < _CACHE_TTL:
                return plan_info
    # Cache miss or expired — hit DB via PgPool.checkout() context-manager
    # (the only public connection API on PgPool; getconn/putconn are private).
    with pg_pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.slug, p.quota_calls_per_month, p.rate_limit_rpm,
                       k.rate_limit_override, k.quota_override
                  FROM plans p
                  JOIN api_keys k ON k.plan_id = p.id
                 WHERE k.id = %s
                """,
                (api_key_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise KeyError(f"No plan found for api_key_id={api_key_id}")
    plan_info = PlanInfo(
        plan_id=row[0],
        slug=row[1],
        quota_calls_per_month=row[2],
        rate_limit_rpm=row[3],
        rate_limit_override=row[4],  # None if NULL in DB (M10B P0-ext)
        quota_override=row[5],       # None if NULL in DB (M10B P0-ext)
    )
    with _cache_lock:
        _PLAN_CACHE[api_key_id] = (plan_info, now)
    return plan_info


def _check_monthly_quota(
    api_key_id: int, plan_info: "PlanInfo", pg_pool
) -> tuple[bool, int, int]:
    """Check monthly call quota for the key. Returns (allowed, used, quota).

    Bypass paths (fail-open for monthly quota):
      1. slug='unlimited' (ADR-0041 D5 SSOT) — bypass without DB query.
      2. slug='__fallback__' (degraded-mode plan when DB is unreachable) — bypass
         monthly gate so authenticated requests are not blocked during DB outage.
         RPM is still enforced via plan.rate_limit_rpm because '__fallback__' does
         NOT match the 'unlimited' SSOT in _resolve_effective_rpm — the RPM bucket
         consults plan.rate_limit_rpm = DEFAULT_RATE_LIMIT_RPM (fail-safe).

    The dual-slug bypass in path 2 is the R-6-A fix for BLOCK-1 (R-5 review):
    R-4-A's BLOCK-2 fix changed the fallback plan slug to 'unlimited' to preserve
    monthly fail-open, but that silently caused RPM bypass too. Restoring
    '__fallback__' + adding the explicit slug check here decouples the two gates.

    Args:
        api_key_id: The authenticated API key id.
        plan_info:  PlanInfo with quota_calls_per_month + override fields.
        pg_pool:    psycopg2 connection pool.

    Returns:
        (allowed, used, quota) — quota=0 means unlimited in bypass paths.
    """
    effective_quota, is_unlimited = _resolve_effective_quota(plan_info)
    if is_unlimited or plan_info.slug == "__fallback__":
        # SSOT 'unlimited' (ADR-0041 D5) + degraded-mode '__fallback__' both
        # fail-open for monthly quota. RPM is enforced via plan.rate_limit_rpm
        # in _check_rate_limit because '__fallback__' is NOT the 'unlimited' slug
        # SSOT in _resolve_effective_rpm.
        return True, 0, 0

    # BLOCK-2 fix (preserved): the old `if effective_quota == 0` numeric guard
    # was removed. Previously, effective_quota=0 after override resolution triggered
    # an unlimited bypass regardless of is_unlimited.  That meant quota_override=0
    # on a non-unlimited plan silently granted unlimited access — contradicting
    # ADR-0041 D5 ("0 = zero-allowed, NOT unlimited").  The dual-slug bypass above
    # is now the sole fail-open path.  For pre-M10B plan rows seeded with quota=0
    # that are intended to be unlimited, migrate those rows to slug='unlimited'
    # or set quota_calls_per_month > 0.

    # PgPool.checkout() is the only public connection API; getconn/putconn
    # are private to PgPool internals.
    with pg_pool.checkout() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT call_count
                  FROM usage_counter
                 WHERE api_key_id = %s
                   AND period_yyyymm = to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')
                """,
                (api_key_id,),
            )
            row = cur.fetchone()

    used = row[0] if row else 0
    return used < effective_quota, used, effective_quota


async def _flush_usage_buffer_async(pg_pool) -> None:
    """Flush accumulated increments from _usage_buffer to usage_counter (DB).

    Uses atomic UPSERT: INSERT ... ON CONFLICT DO UPDATE so concurrent flushes
    from multiple processes are safe. Runs in a thread via asyncio.to_thread to
    avoid blocking the event loop.

    Best-effort: logs on error, never raises.
    """
    with _usage_buffer_lock:
        if not _usage_buffer:
            return
        snapshot = dict(_usage_buffer)
        _usage_buffer.clear()

    def _do_flush():
        # PgPool.checkout() yields a clean conn with autocommit=True. We flip
        # autocommit OFF for the duration of the flush so the per-key UPSERTs
        # commit atomically (or roll back as a batch on error), preserving
        # the original transaction semantics. checkout()'s finally clause
        # returns the conn to the pool; the next caller's rollback() reset
        # will clear any leftover txn state.
        with pg_pool.checkout() as conn:
            try:
                conn.autocommit = False
                with conn.cursor() as cur:
                    for key_id, delta in snapshot.items():
                        if delta <= 0:
                            continue
                        cur.execute(
                            """
                            INSERT INTO usage_counter
                                (api_key_id, period_yyyymm, call_count, updated_at)
                            VALUES
                                (%s, to_char(now() AT TIME ZONE 'UTC', 'YYYYMM'), %s, now())
                            ON CONFLICT (api_key_id, period_yyyymm) DO UPDATE
                                SET call_count  = usage_counter.call_count + EXCLUDED.call_count,
                                    updated_at  = now()
                            """,
                            (key_id, delta),
                        )
                conn.commit()
            except Exception as exc:
                _logger.warning("usage_buffer flush error: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass

    try:
        await asyncio.to_thread(_do_flush)
    except Exception as exc:
        _logger.warning("usage_buffer flush thread error: %s", exc)


def _increment_usage_buffer(api_key_id: int, pg_pool) -> bool:
    """Increment in-process buffer for api_key_id; return True when flush triggered.

    Flush is scheduled (fire-and-forget) when total pending across all keys
    hits _USAGE_FLUSH_THRESHOLD. The flush task is added to _BG_TASKS to
    prevent GC-before-completion (B3 pattern).
    """
    with _usage_buffer_lock:
        _usage_buffer[api_key_id] = _usage_buffer.get(api_key_id, 0) + 1
        total_pending = sum(_usage_buffer.values())

    if total_pending >= _USAGE_FLUSH_THRESHOLD:
        return True  # caller schedules the flush
    return False


# Strong references to background tasks prevent GC-before-completion (B3).
_BG_TASKS: set[asyncio.Task] = set()

# Paths that bypass auth entirely
_PUBLIC_PATHS = frozenset({"/health", "/ready", "/metrics"})
_PUBLIC_PATH_PREFIXES = frozenset({"/install"})


def _cache_get(raw_key: str) -> tuple[bool, int | None]:
    """Return (hit, api_key_id). hit=False means cache miss or expired.

    Thread-safe: guarded by _cache_lock.
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        ts = _CACHE_TS.get(h)
        if ts is not None and time.monotonic() - ts < _CACHE_TTL:
            return True, _KEY_CACHE.get(h)
        return False, None


def _cache_set(raw_key: str, key_id: int | None) -> None:
    """Store key_id for raw_key (stored as hash) in the in-memory cache.

    Thread-safe: guarded by _cache_lock.
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        _KEY_CACHE[h] = key_id
        _CACHE_TS[h] = time.monotonic()


def _cache_invalidate(raw_key: str) -> None:
    """Remove a key from cache (call after deactivate with raw_key known).

    Thread-safe: guarded by _cache_lock.
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        _KEY_CACHE.pop(h, None)
        _CACHE_TS.pop(h, None)
        _TENANT_CACHE.pop(h, None)  # keep the caches in sync (see _cache_set_tenant)
        _OWNER_CACHE.pop(h, None)   # owner-meta cache shares the same lifetime


def _cache_invalidate_by_key_id(key_id: int) -> None:
    """Remove all cache entries mapping to key_id (call after deactivate).

    Used when only key_id is available (e.g. Web UI deactivate route).
    O(n) scan is fine — cache holds at most a few hundred entries.
    Works in-process; cross-process invalidation is bounded by _CACHE_TTL.

    Also drops the matching _PLAN_CACHE entry so admin plan reassignment
    (PATCH api_keys.plan_id) takes effect immediately instead of waiting
    for the 300s TTL (B2 follow-up — Wave 2 integration review ISSUE-2).

    Thread-safe: guarded by _cache_lock.
    """
    with _cache_lock:
        stale = [h for h, v in _KEY_CACHE.items() if v == key_id]
        for h in stale:
            _KEY_CACHE.pop(h, None)
            _CACHE_TS.pop(h, None)
            _TENANT_CACHE.pop(h, None)
            _OWNER_CACHE.pop(h, None)
        _PLAN_CACHE.pop(key_id, None)


def _cache_set_tenant(raw_key: str, tenant_id: int | None) -> None:
    """Store tenant_id for raw_key (stored as hash) in the tenant cache.

    Must be called alongside _cache_set so the two caches stay in sync.
    Thread-safe: guarded by _cache_lock (same lock as _KEY_CACHE).
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        _TENANT_CACHE[h] = tenant_id


def _cache_get_tenant(raw_key: str) -> tuple[bool, int | None]:
    """Return (hit, tenant_id) from tenant cache. hit=False means miss or expired.

    Uses _CACHE_TS (shared with _KEY_CACHE) for TTL — the two caches have the
    same lifetime so a single timestamp dict is correct.
    Thread-safe: guarded by _cache_lock.
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        ts = _CACHE_TS.get(h)
        if ts is not None and time.monotonic() - ts < _CACHE_TTL:
            # Tenant cache may not have an entry if it was set by an old code path
            # that did not call _cache_set_tenant.  Return (True, None) in that case
            # so the caller treats a global key correctly.
            return True, _TENANT_CACHE.get(h)
        return False, None


def _cache_set_owner(raw_key: str, user_id: int | None, owner_is_admin: bool) -> None:
    """Store owner metadata (user_id, owner_is_admin) for raw_key in _OWNER_CACHE.

    Must be called alongside _cache_set / _cache_set_tenant so all caches stay in
    sync. Thread-safe: guarded by _cache_lock (same lock as _KEY_CACHE).
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        _OWNER_CACHE[h] = (user_id, bool(owner_is_admin))


def _cache_get_owner(raw_key: str) -> tuple[bool, int | None, bool]:
    """Return (hit, user_id, owner_is_admin) from _OWNER_CACHE.

    hit=False means miss/expired OR the TTL entry exists but no owner metadata was
    written for this key (e.g. a verify path that predates verify_api_key_full).
    Treating the latter as a miss is the fail-closed choice: the middleware then
    re-verifies via DB rather than skipping the read-side guard with unknown
    owner state. Mirrors the _TENANT_CACHE split fail-closed handling.

    Thread-safe: guarded by _cache_lock.
    """
    h = _hash_key(raw_key)
    with _cache_lock:
        ts = _CACHE_TS.get(h)
        if ts is not None and time.monotonic() - ts < _CACHE_TTL:
            cached = _OWNER_CACHE.get(h)
            if cached is None:
                # TTL valid but no owner metadata recorded → treat as miss so the
                # caller re-verifies (fail-closed, not fail-open).
                return False, None, False
            user_id, owner_is_admin = cached
            return True, user_id, owner_is_admin
        return False, None, False


def _is_null_tenant_escalation(
    tenant_id: int | None, user_id: int | None, owner_is_admin: bool
) -> bool:
    """Read-side invariant check (defense-in-depth, ADR-0034 follow-up).

    Returns True when the presented key is in the invalid "unrestricted" state it
    must NEVER be in — and the request must therefore be rejected fail-closed:

        user-owned (user_id IS NOT NULL)
        AND owner is non-admin (owner_is_admin == False)
        AND tenant_id IS NULL  (the unrestricted/admin sentinel)

    Such a key would read across every tenant despite belonging to a non-admin
    user — the exact exposure this branch's write-side mint/reactivate/reassign
    fixes prevent. This is the complementary read-side guard.

    Legitimately-unrestricted keys (NOT flagged):
      - system/CLI keys           (user_id IS NULL)            → never owned
      - admin-owned keys          (owner_is_admin == True)     → admin may roam
      - any tenant-scoped key     (tenant_id IS NOT NULL)      → already scoped

    Fail-closed posture: owner_is_admin is supplied already coerced to a strict
    bool (NULL is_admin → False at the SQL/cache boundary), so an undeterminable
    admin flag is treated as non-admin (deny), never as admin (allow).
    """
    return user_id is not None and not owner_is_admin and tenant_id is None


class AuthMiddleware(BaseHTTPMiddleware):
    """Verify X-API-Key header on every request except public paths."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Public paths bypass auth (exact match or prefix match)
        if request.url.path in _PUBLIC_PATHS or any(
            request.url.path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES
        ):
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            return Response("Missing X-API-Key header", status_code=401)

        # Check cache first to avoid DB round-trip per request.
        # Fail-closed: ALL THREE caches must hit for a cache-served response.
        # If _KEY_CACHE hits but _TENANT_CACHE or _OWNER_CACHE misses (e.g. a
        # post-deploy window where old code wrote key_id but not tenant_id /
        # owner-meta), we do NOT fall back to tenant_id=None / unknown owner —
        # that would silently escalate a tenant key to admin/unscoped scope
        # (cross-tenant read) OR skip the read-side guard. Instead we treat it as
        # a full miss and go to DB so all three are repopulated in one round-trip.
        hit, key_id = _cache_get(raw_key)
        tenant_hit, tenant_id = _cache_get_tenant(raw_key)
        owner_hit, owner_user_id, owner_is_admin = _cache_get_owner(raw_key)
        if not hit or not tenant_hit or not owner_hit:
            def _do_verify():
                from src.db.pg import auth_store
                store = auth_store()
                # Prefer verify_api_key_full to retrieve key_id + tenant_id +
                # user_id + owner_is_admin in a SINGLE DB query (read-side guard,
                # ADR-0034 follow-up). It supersedes verify_api_key_tenant on the
                # hot path; the older methods remain as graceful fallbacks for:
                #   (a) a legacy store / test stub lacking the newer method,
                #   (b) a non-(4|2)-tuple return (e.g. MagicMock auto-attribute),
                #   (c) a rolling deploy against a pre-tenant schema.
                # Fallback paths cannot determine owner metadata, so they return
                # user_id=None / owner_is_admin=False — which the read-side guard
                # treats as "not an escalation" (user_id None ⇒ allowed). That
                # keeps existing tests/stubs green while the primary write-side
                # invariant remains the first line of defence.
                _missing = object()

                # 1) verify_api_key_full → 4-tuple (key_id, tenant_id, user_id, is_admin)
                full: object = _missing
                try:
                    full = store.verify_api_key_full(raw_key)
                except AttributeError:
                    pass  # method not present — fall through
                except psycopg2.errors.UndefinedColumn:
                    pass  # pre-tenant / pre-is_admin schema — fall through
                if full is not _missing:
                    if full is None or (isinstance(full, tuple) and len(full) == 4):
                        return full  # 4-tuple or None — correct type
                    # Unexpected type (MagicMock auto-attr in a stub mocking only
                    # the older methods) — fall through to verify_api_key_tenant.

                # 2) verify_api_key_tenant → 2-tuple (key_id, tenant_id)
                result: object = _missing
                try:
                    result = store.verify_api_key_tenant(raw_key)
                except AttributeError:
                    pass  # method not present — fall through to verify_api_key
                except psycopg2.errors.UndefinedColumn:
                    # api_keys.tenant_id absent — new code running against a
                    # pre-m13_002 schema (e.g. a rolling deploy before migrate
                    # has applied). Degrade to legacy verify_api_key
                    # (tenant_id=None) instead of 500-ing every authed request.
                    pass
                if result is not _missing:
                    if result is None:
                        return None
                    if isinstance(result, tuple) and len(result) == 2:
                        # Owner metadata unknown via this path → (None, False).
                        _kid, _tid = result
                        return (_kid, _tid, None, False)
                    # Unexpected return type (e.g. MagicMock auto-attribute in unit
                    # tests that only mock verify_api_key).  Fall through below.

                # 3) legacy verify_api_key → key_id | None (may raise
                #    OperationalError — propagates to the caller's except clause).
                key_id_only = store.verify_api_key(raw_key)
                if key_id_only is None:
                    return None
                return (key_id_only, None, None, False)

            try:
                result = await asyncio.to_thread(_do_verify)
            except (PoolNotInitializedError, psycopg2.OperationalError) as e:
                # Degraded mode: pool not initialised (lifespan retry still
                # running) OR a transient DB outage. Return 503 with a
                # static body — see _degraded_response docstring re CWE-209.
                # Public paths (/health, /install) already bypassed this
                # branch above.
                #
                # Narrowed to PoolNotInitializedError (not bare RuntimeError)
                # so unrelated runtime errors from auth_store / framework
                # surface as 500, not silently get masked as degraded.
                _logger.warning(
                    "auth path degraded — returning 503 to %s %s. Cause: %s",
                    request.method, request.url.path, str(e)[:300],
                )
                return _degraded_response()
            if result is None:
                key_id = None
                tenant_id = None
                owner_user_id = None
                owner_is_admin = False
            else:
                key_id, tenant_id, owner_user_id, owner_is_admin = result
            _cache_set(raw_key, key_id)
            _cache_set_tenant(raw_key, tenant_id)
            _cache_set_owner(raw_key, owner_user_id, owner_is_admin)

        if key_id is None:
            return Response("Invalid or inactive API key", status_code=401)

        # Read-side authorization guard (defense-in-depth, ADR-0034 follow-up).
        # Reject fail-closed a user-owned, NON-admin key that carries tenant_id
        # IS NULL — the "unrestricted" sentinel reserved for system/CLI keys and
        # admin-owned keys. Such a key should never exist (the write-side
        # mint/reactivate/reassign fixes prevent it), but if some future path
        # leaves one, this guard ensures it cannot read across tenants. Uses the
        # SAME fail-closed response as an invalid key.
        #
        # Privilege-persistence on admin demotion is closed by this guard ONLY in
        # conjunction with set_user_admin's write-side fixes: on demote it (a)
        # re-scopes the user's active NULL-tenant keys to a concrete tenant in the
        # same transaction, and (b) the route invalidates this per-key owner cache.
        # The guard alone does NOT close the gap, because owner_is_admin is cached
        # for _CACHE_TTL (300 s); without the demote-time cache invalidation a
        # just-demoted admin would keep serving owner_is_admin=True (so this guard
        # would not fire) for up to the TTL window.
        if _is_null_tenant_escalation(tenant_id, owner_user_id, owner_is_admin):
            _logger.warning(
                "read-side guard: rejecting user-owned non-admin key with "
                "tenant_id IS NULL (key_id=%s, user_id=%s) — invalid unrestricted "
                "state, denying.",
                key_id, owner_user_id,
            )
            return Response("Invalid or inactive API key", status_code=401)

        # Plan-aware rate limiting + monthly quota (WI-B2).
        # Fetch plan info from cache / DB. On error (plan row missing or DB down),
        # fall back to DEFAULT_RATE_LIMIT_RPM so a transient lookup failure doesn't
        # block all requests. Log the anomaly for ops.
        import datetime as _dt

        from src.db.pg import get_pool as _get_pool
        pg_pool = None
        plan_info: PlanInfo | None = None
        try:
            pg_pool = _get_pool()
            plan_info = _get_plan_for_key(key_id, pg_pool)
        except Exception as _plan_exc:
            _logger.warning(
                "plan_lookup failed for key_id=%s — falling back to defaults. Error: %s",
                key_id, str(_plan_exc)[:200],
            )
            plan_info = PlanInfo(
                plan_id=0,
                slug="__fallback__",  # R-6-A fix (BLOCK-1): NOT 'unlimited' — RPM must
                                      # be enforced during DB outage. Using 'unlimited'
                                      # (R-4-A's BLOCK-2 fix) caused _resolve_effective_rpm
                                      # to bypass the bucket entirely because slug='unlimited'
                                      # is the SSOT for ALL bypass paths including RPM.
                                      # With '__fallback__', _resolve_effective_rpm falls
                                      # through to return plan.rate_limit_rpm →
                                      # DEFAULT_RATE_LIMIT_RPM is enforced (fail-safe RPM
                                      # during degraded mode). Monthly quota fail-open is
                                      # preserved via an explicit dual-slug guard in
                                      # _check_monthly_quota:
                                      #   `is_unlimited or plan_info.slug == '__fallback__'`
                quota_calls_per_month=0,
                rate_limit_rpm=DEFAULT_RATE_LIMIT_RPM,
                rate_limit_override=None,
                quota_override=None,
            )

        allowed_rpm, remaining = _check_rate_limit(key_id, plan_info)
        if not allowed_rpm:
            now_utc = _dt.datetime.now(_dt.UTC)
            reset_at = (now_utc + _dt.timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # R-9 fix: redact internal sentinel slugs (e.g. '__fallback__' for degraded
            # mode) from the public 429 body — they were enforcement discriminators,
            # never intended as user-visible plan labels. Per ADR-0041 D5, only DB-
            # sourced plan slugs are user-facing. The `startswith("__")` guard covers
            # any future internal sentinel by naming convention.
            _body_payload = {
                "status": "quota_exhausted",
                "reason": "rpm",
                "reset_at": reset_at,
            }
            if not plan_info.slug.startswith("__"):
                _body_payload["plan"] = plan_info.slug
            body = _json.dumps(_body_payload)
            # Wave 2 integration review ISSUE-4 — emit X-Quota-Limit +
            # X-Quota-Period on RPM 429 for ops-dashboard parity with the
            # monthly 429 branch (grep `X-Quota-*` across all 429s now hits
            # both reasons).  X-Quota-Used omitted intentionally — fetching
            # the current monthly counter here would add a DB round-trip on
            # the throttled-request hot path; the dashboard derives "used"
            # from `usage_counter` directly when needed.
            # M10B P0-ext: X-Quota-Limit emits "unlimited" string sentinel
            # when plan is unlimited (clearest semantics per A8 Option 1).
            _eff_q, _is_unl_q = _resolve_effective_quota(plan_info)
            _quota_limit_hdr = "unlimited" if _is_unl_q else str(_eff_q)
            return Response(
                body,
                status_code=429,
                media_type="application/json",
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-Quota-Limit": _quota_limit_hdr,
                    "X-Quota-Period": now_utc.strftime("%Y%m"),
                },
            )

        allowed_monthly = True
        used_monthly = 0
        quota_monthly = 0
        if pg_pool is not None:
            try:
                allowed_monthly, used_monthly, quota_monthly = _check_monthly_quota(
                    key_id, plan_info, pg_pool
                )
            except Exception as _quota_exc:
                _logger.warning(
                    "monthly_quota check failed for key_id=%s — allowing request. Error: %s",
                    key_id, str(_quota_exc)[:200],
                )

        if not allowed_monthly:
            now_utc = _dt.datetime.now(_dt.UTC)
            # Reset at start of next UTC calendar month
            if now_utc.month == 12:
                next_month = now_utc.replace(year=now_utc.year + 1, month=1, day=1,
                                             hour=0, minute=0, second=0, microsecond=0)
            else:
                next_month = now_utc.replace(month=now_utc.month + 1, day=1,
                                             hour=0, minute=0, second=0, microsecond=0)
            reset_at = next_month.strftime("%Y-%m-%dT%H:%M:%SZ")
            # NIT fix: emit "unlimited" sentinel on monthly 429 X-Quota-Limit when
            # is_unlimited, consistent with RPM 429 + success 200 paths (A:632/C:C8).
            # R-8 fix: include '__fallback__' for symmetry with the success path,
            # even though this branch is structurally unreachable today (L257 dual-
            # slug bypass returns allowed=True before this code runs). Defensive
            # against a future refactor that re-enters this branch with the
            # fallback plan and would otherwise emit "0".
            _eff_q_m, _is_unl_m = _resolve_effective_quota(plan_info)
            _monthly_bypassed_m = _is_unl_m or plan_info.slug == "__fallback__"
            _quota_limit_m = "unlimited" if _monthly_bypassed_m else str(quota_monthly)
            # R-9 fix: same sentinel-redact pattern as RPM 429 (see FIX-1 comment).
            # This branch is structurally unreachable today for slug='__fallback__'
            # (L257 dual-slug short-circuit), but pin the invariant defensively
            # against a future refactor that re-enters this branch with a sentinel slug.
            _body_payload = {
                "status": "quota_exhausted",
                "reason": "monthly",
                "used": used_monthly,
                "quota": quota_monthly,
                "reset_at": reset_at,
            }
            if not plan_info.slug.startswith("__"):
                _body_payload["plan"] = plan_info.slug
            body = _json.dumps(_body_payload)
            return Response(
                body,
                status_code=429,
                media_type="application/json",
                headers={
                    "X-RateLimit-Remaining": str(remaining),
                    "X-Quota-Used": str(used_monthly),
                    "X-Quota-Limit": _quota_limit_m,
                    "X-Quota-Period": _dt.datetime.now(_dt.UTC).strftime("%Y%m"),
                },
            )

        request.state.api_key_id = key_id
        request.state.tenant_id = tenant_id  # ADR-0034 D4.1 — None for global/admin keys
        # Store key_prefix (first 12 chars of raw key) for audit actor resolution.
        # Derived from raw_key in-process — zero extra DB query on the hot path.
        request.state.key_prefix = raw_key[:12]
        start = time.monotonic()
        response = await call_next(request)
        ms = int((time.monotonic() - start) * 1000)

        # Inject quota headers on allowed responses.
        # M10B P0-ext: X-Quota-Limit emits "unlimited" string sentinel when
        # plan is unlimited (ADR-0041 D5 / A8 Option 1 — clearest semantics).
        # R-8 fix: also emit "unlimited" when slug='__fallback__' (degraded-mode
        # DB outage). The enforcement path (_check_monthly_quota L257) already
        # uses a dual-slug bypass; the observability path must match or the
        # header will show "0" while the request was actually bypassed.
        now_period = _dt.datetime.now(_dt.UTC).strftime("%Y%m")
        _eff_q_hdr, _is_unl_hdr = _resolve_effective_quota(plan_info)
        _monthly_bypassed = _is_unl_hdr or plan_info.slug == "__fallback__"
        _quota_limit_val = "unlimited" if _monthly_bypassed else str(quota_monthly)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-Quota-Used"] = str(used_monthly)
        response.headers["X-Quota-Limit"] = _quota_limit_val
        response.headers["X-Quota-Period"] = now_period

        # Increment usage buffer; schedule flush if threshold reached.
        if pg_pool is not None:
            should_flush = _increment_usage_buffer(key_id, pg_pool)
            if should_flush:
                task_flush = asyncio.create_task(_flush_usage_buffer_async(pg_pool))
                _BG_TASKS.add(task_flush)
                task_flush.add_done_callback(_BG_TASKS.discard)

        # Fire-and-forget usage log — hold strong ref to prevent GC (B3)
        task = asyncio.create_task(_log_usage_async(key_id, request, ms))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return response


async def _log_usage_async(key_id: int, request: Request, ms: int) -> None:
    """HTTP-level request trace — best-effort, never raises.

    Note: The DB insert into usage_log is handled by UsageLogMiddleware
    (src/mcp/tool_log_middleware.py) at the FastMCP layer, where
    context.message.name gives the actual MCP tool name.  This function
    only emits an HTTP-level log line for ops tracing; it no longer writes
    to the DB so that we avoid double-inserts and the tool_name='unknown' bug.
    """
    try:
        _logger.info("http_request key_id=%s ms=%d path=%s", key_id, ms, request.url.path)
    except Exception:
        pass
