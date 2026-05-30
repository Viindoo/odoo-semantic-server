# SPDX-License-Identifier: AGPL-3.0-or-later
"""Data-plane provisioning for billing entitlements (M10B P1, ADR-0039 D3).

This module turns a *claimed* subscription into concrete access: it upgrades (or
creates) the buyer's API key to the purchased plan, and — for multi-seat
purchases — provisions a tenant the buyer administers.

It is deliberately thin: every key/tenant/membership write reuses an existing
``AuthStore`` helper.  Provisioning NEVER reinvents key minting, plan
assignment, or tenant creation; it orchestrates the helpers + the subscription
registry, and flushes the MCP middleware plan cache so a plan change takes
effect immediately rather than after the 300s TTL.

Two entry points:

* :func:`provision_or_upgrade` — given a *claimed* subscription + its owner,
  bring the owner's access up to the purchased plan (highest-tier-wins: never
  downgrades a buyer who already holds a more expensive plan).
* :func:`claim_subscription_for_user` — claim-on-login hook.  Best-effort: it
  NEVER raises into the auth flow (same contract as ``_mint_default_api_key``).
"""
import logging

from src.db.auth_registry import set_api_key_plan_and_overrides
from src.db.pg import auth_store, get_pool, subscription_store

logger = logging.getLogger(__name__)

# ADR-0041 D5: the 'unlimited' SLUG is the SSOT for unlimited access.  It is
# seeded at price_cents=0 (same as 'free'), so price alone CANNOT distinguish
# it.  Plan rank therefore puts the unlimited-sentinel slug above every priced
# plan as a top sentinel; all other plans rank by price_cents.  A key on the
# sentinel plan (admin-granted) must NEVER be downgraded by a Polar grant/update.
#
# The sentinel slug is admin-configurable via ``billing.unlimited_sentinel_slug``
# (default 'unlimited') so a renamed sentinel still protects the top tier; this
# constant is only the fall-back default when the setting is unavailable.
_UNLIMITED_SLUG = "unlimited"
# Rank above any realistic price_cents so the sentinel always wins.
_UNLIMITED_RANK = 1 << 62


def _unlimited_sentinel_slug() -> str:
    """Return the admin-configured unlimited-sentinel slug (default 'unlimited').

    Read via ``get_setting`` (60s TTL cache) so renaming the sentinel in admin
    settings keeps the top-tier no-downgrade protection without a redeploy.
    Falls back to :data:`_UNLIMITED_SLUG` if the setting resolves to nothing.
    """
    from src.settings import get_setting

    return get_setting("billing.unlimited_sentinel_slug") or _UNLIMITED_SLUG


def _plan_rank(*, slug: str | None, price_cents: int | None) -> int:
    """Tier rank for highest-tier-wins: the unlimited-sentinel slug is the top.

    The unlimited-sentinel slug outranks every priced plan (ADR-0041 D5); all
    other plans rank by ``price_cents`` (0 for free).  ``None`` price → 0.  The
    sentinel slug is read from ``billing.unlimited_sentinel_slug`` so a renamed
    sentinel still wins.
    """
    if slug is not None and slug == _unlimited_sentinel_slug():
        return _UNLIMITED_RANK
    return price_cents or 0


def _plan_meta(conn, plan_id: int) -> tuple[str | None, int]:
    """Return (slug, price_cents) for ``plan_id`` using an open ``conn``.

    Missing plan row → (None, 0).  Read-only, fully parameterized.
    """
    pool = get_pool()
    row = pool.fetch_one(
        conn, "SELECT slug, price_cents FROM plans WHERE id = %s", (plan_id,)
    )
    if row is None:
        return None, 0
    return row["slug"], row["price_cents"]


def _key_plan_meta(conn, key_id: int) -> tuple[str | None, int] | None:
    """Return (slug, price_cents) of the plan on ``key_id``, or None if unresolved.

    ``None`` means the key (or its plan) could not be resolved — caller treats
    that as "no incumbent plan to protect".  Read-only, fully parameterized.
    """
    pool = get_pool()
    row = pool.fetch_one(
        conn,
        "SELECT p.slug AS slug, p.price_cents AS price_cents "
        "FROM api_keys k JOIN plans p ON p.id = k.plan_id "
        "WHERE k.id = %s",
        (key_id,),
    )
    if row is None:
        return None
    return row["slug"], row["price_cents"]


def _plan_outranks(current_plan_id: int, new_plan_id: int) -> bool:
    """Return True if the CURRENT plan strictly outranks the NEW plan.

    When True, switching to ``new_plan_id`` would be a DOWNGRADE and must be
    refused (highest-tier-wins).  Uses :func:`_plan_rank` so the 'unlimited'
    slug always beats any priced plan (ADR-0041 D5).  Shared by both the grant
    path (``provision_or_upgrade``) and ``activation.update_entitlement`` so
    neither ever downgrades a higher tier.  One pool checkout, both lookups.
    """
    if current_plan_id == new_plan_id:
        return False
    pool = get_pool()
    with pool.checkout() as conn:
        cur_slug, cur_price = _plan_meta(conn, current_plan_id)
        new_slug, new_price = _plan_meta(conn, new_plan_id)
    cur_rank = _plan_rank(slug=cur_slug, price_cents=cur_price)
    new_rank = _plan_rank(slug=new_slug, price_cents=new_price)
    return cur_rank > new_rank


def provision_or_upgrade(subscription_id: int, user_id: int) -> int:
    """Provision access for a *claimed* subscription. Returns the api_key_id.

    Steps (each reuses an existing helper — no key/tenant logic is reinvented):

    1. Resolve the subscription + its ``plan_id``.
    2. Find the user's ACTIVE API key (``list_api_keys(user_id, admin=False)``,
       preferring ``active=TRUE`` — never a deactivated key):
       - active key exists → upgrade IN PLACE via ``set_api_key_plan_and_overrides``
         with ``update_*_override=False`` so per-key overrides are left untouched.
       - none active → ``create_api_key`` then set the plan on the fresh key.
       HIGHEST-TIER-WINS (ADR-0041 D5): if the active key is already on a plan
       that OUTRANKS the purchased plan (``_plan_outranks`` — the ``unlimited``
       slug beats any priced plan; otherwise by ``price_cents``), the plan is
       NOT changed (no downgrade); the existing key is still linked to the sub.
    3. ``seats > 1`` → ``create_tenant("sub-<id>")`` (idempotent-guarded),
       ``add_tenant_member(user_id, tenant_id, 'tenant_admin')``,
       ``link_to_tenant``.
    4. Link the api_key to the subscription FIRST, then set ``claimed_user_id``
       LAST.  Invariant (I7): ``claimed_user_id`` is set ONLY once ``api_key_id``
       is also set — so any failure before this point leaves ``claimed_user_id``
       NULL and the sub is re-surfaced by ``find_unclaimed_active_by_email`` for
       a retry (buyer never paid for a key they can't get).
    5. Flush the middleware plan cache for the key.

    Raises if the subscription does not exist (programmer error: a claim path
    must pass a real subscription_id).
    """
    store = auth_store()
    subs = subscription_store()
    pool = get_pool()

    sub = subs.get_by_id(subscription_id)
    if sub is None:
        raise ValueError(f"provision_or_upgrade: subscription id={subscription_id} not found")
    plan_id: int = sub["plan_id"]
    seats: int = sub["seats"] or 1

    # ---- 1. resolve / create the user's ACTIVE api key ----------------------
    # list_api_keys is ordered by id ASC and includes deactivated keys; pick the
    # FIRST key with active=TRUE so a paid plan never lands on a dead key while
    # the user's real key stays on free (I6).
    existing = store.list_api_keys(user_id=user_id, admin=False)
    active_key = next((k for k in existing if k.get("active")), None)
    if active_key is not None:
        key_id = active_key["id"]
        # Highest-tier-wins in ONE checkout: fetch the active key's current plan
        # meta AND the purchased plan's meta together, then rank (I17 + I4).
        with pool.checkout() as conn:
            cur_meta = _key_plan_meta(conn, key_id)
            new_slug, new_price = _plan_meta(conn, plan_id)
        new_rank = _plan_rank(slug=new_slug, price_cents=new_price)
        cur_rank = (
            _plan_rank(slug=cur_meta[0], price_cents=cur_meta[1])
            if cur_meta is not None
            else -1  # unresolved incumbent → nothing to protect, allow upgrade
        )
        if cur_rank > new_rank:
            logger.info(
                "provision_or_upgrade: keeping higher-tier plan on key_id=%d "
                "(current plan outranks purchased plan_id=%d); not downgrading",
                key_id, plan_id,
            )
        else:
            set_api_key_plan_and_overrides(
                pool, key_id, plan_id, None, None,
                update_rate_limit_override=False,
                update_quota_override=False,
            )
    else:
        # No active key (fresh user OR all keys deactivated): mint one, then set
        # the purchased plan. create_api_key omits plan_id → DB DEFAULT=free, so
        # the explicit plan assignment below is what makes it the paid tier.
        username = _username_for(user_id)
        label = f"sub-{subscription_id} ({username})"
        _raw, _prefix, key_id = store.create_api_key(name=label, user_id=user_id)
        set_api_key_plan_and_overrides(
            pool, key_id, plan_id, None, None,
            update_rate_limit_override=False,
            update_quota_override=False,
        )

    # ---- 2. multi-seat → tenant + tenant_admin membership -------------------
    if seats > 1:
        tenant_id = _ensure_tenant(subscription_id)
        store.add_tenant_member(user_id, tenant_id, "tenant_admin")
        subs.link_to_tenant(subscription_id, tenant_id)

    # ---- 3. link api_key FIRST, then claimed_user_id LAST (I7 invariant) ----
    # If anything above raised, claimed_user_id is still NULL → the sub stays
    # retryable via find_unclaimed_active_by_email.
    subs.link_to_api_key(subscription_id, key_id)
    subs.link_to_user(subscription_id, user_id)

    # ---- 4. flush the middleware plan cache so the change is immediate -------
    _invalidate_plan_cache(key_id)

    return key_id


def claim_subscription_for_user(user_id: int, email: str) -> list[int]:
    """Claim-on-login hook: claim + provision any unclaimed paid subs for ``email``.

    Best-effort by contract — this is wired into signup/oauth/password-login and
    MUST NEVER raise into the auth flow (same posture as ``_mint_default_api_key``).
    Any exception is caught, logged, and the partial result returned.

    Returns the list of provisioned ``api_key_id`` values (possibly empty).
    """
    provisioned: list[int] = []
    try:
        subs = subscription_store()
        candidates = subs.find_unclaimed_active_by_email(email)
        for sub in candidates:
            try:
                sub_id = sub["id"]
                # provision_or_upgrade owns the claim and sets claimed_user_id
                # LAST (I7); do NOT pre-link here, or a mid-provision failure
                # would orphan the sub (claimed but no key → never re-surfaced).
                key_id = provision_or_upgrade(sub_id, user_id)
                provisioned.append(key_id)
            except Exception as exc:  # noqa: BLE001 — one bad sub must not abort the rest
                logger.warning(
                    "claim_subscription_for_user: failed to provision sub_id=%s "
                    "for user_id=%d: %s",
                    sub.get("id"), user_id, exc,
                )
        if provisioned:
            logger.info(
                "claim_subscription_for_user: provisioned %d subscription(s) for user_id=%d",
                len(provisioned), user_id,
            )
    except Exception as exc:  # noqa: BLE001 — never raise into the auth flow
        logger.warning(
            "claim_subscription_for_user: claim sweep failed for user_id=%d (email=%r): %s",
            user_id, email, exc,
        )
    return provisioned


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_tenant(subscription_id: int) -> int:
    """Create (idempotently) the tenant for a multi-seat subscription.

    Tenant name is the stable token ``sub-<subscription_id>``.  ``create_tenant``
    raises ``UniqueViolation`` on a duplicate name, so we treat that as
    "already provisioned" and look the existing tenant up by name.
    """
    import psycopg2

    store = auth_store()
    name = f"sub-{subscription_id}"
    try:
        return store.create_tenant(name)
    except psycopg2.errors.UniqueViolation:
        pool = get_pool()
        with pool.checkout() as conn:
            row = pool.fetch_one(
                conn, "SELECT id FROM tenants WHERE name = %s", (name,)
            )
        if row is None:  # pragma: no cover — UniqueViolation implies the row exists
            raise
        return row["id"]


def _username_for(user_id: int) -> str:
    """Best-effort human label for a freshly-minted key (falls back to the id)."""
    try:
        pool = get_pool()
        with pool.checkout() as conn:
            row = pool.fetch_one(
                conn, "SELECT username FROM webui_users WHERE id = %s", (user_id,)
            )
        if row is not None and row.get("username"):
            return row["username"]
    except Exception:  # noqa: BLE001 — label only, never block provisioning
        pass
    return f"user-{user_id}"


def _invalidate_plan_cache(key_id: int) -> None:
    """Flush the MCP middleware plan cache for ``key_id`` (best-effort).

    Imported lazily so this data-plane module does not pull the MCP server
    surface at import time, and so a missing/renamed hook degrades to the 300s
    TTL rather than crashing provisioning.
    """
    try:
        from src.mcp.middleware import _cache_invalidate_by_key_id
        _cache_invalidate_by_key_id(key_id)
    except Exception as exc:  # noqa: BLE001 — cache flush is an optimisation, not correctness
        logger.warning(
            "provisioning: plan-cache invalidation failed for key_id=%d: %s "
            "(change still applies within the cache TTL)",
            key_id, exc,
        )
