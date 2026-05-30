# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor-agnostic entitlement activation contract (M10B P1, ADR-0039 D3).

``grant_entitlement`` / ``update_entitlement`` / ``revoke_entitlement`` are the
**only** writers of entitlement state.  Both the admin Activation API
(``routes/entitlements.py``) and the Polar webhook handler call exclusively
through these three functions; neither touches ``subscriptions`` or ``api_keys``
directly.  This keeps every money-state transition in one auditable place.

Design invariants:

* ``plan_id`` is a resolved ``plans.id`` (integer FK), never a slug — the slug→id
  resolution happens at the boundary (route / webhook) before the
  :class:`EntitlementGrant` is built.
* Limits are NEVER copied onto a subscription; they live in ``plans`` and are
  resolved via ``plan_id`` at runtime, so an admin plan edit propagates to every
  subscriber immediately.
* Every plan change on a *claimed* subscription (grant, update, revoke) flushes
  the middleware plan cache (handled inside ``provisioning`` for grant; here for
  update/revoke) so the new entitlement is enforced without waiting for the TTL.
"""
import logging
from dataclasses import dataclass

from src.billing import provisioning
from src.db.auth_registry import set_api_key_plan_and_overrides
from src.db.pg import auth_store, get_pool, subscription_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntitlementGrant:
    """The single Activation contract input (ADR-0039 D3).

    ``plan_id`` is resolved BEFORE this dataclass is built (vendor product →
    plans.id), so activation never deals in slugs.  Commercial / timeline fields
    are an informational snapshot; the authoritative limits resolve via
    ``plan_id`` at runtime.
    """

    plan_id: int                 # resolved plans.id (NOT a slug)
    external_ref: str            # vendor purchase id — the idempotency anchor
    source: str                  # 'polar' | 'erp' | 'admin' | 'promo'
    seats: int = 1
    buyer_email: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    billing_interval: str | None = None
    current_period_start: object | None = None
    current_period_end: object | None = None
    trial_ends_at: object | None = None


def grant_entitlement(grant: EntitlementGrant) -> int:
    """Activate an entitlement. Idempotent on ``external_ref``. Returns subscription_id.

    1. ``upsert_by_external_ref(status='active', ...)`` — same external_ref always
       resolves to the same subscription row (no duplicate, no double-provision).
    2. If ``buyer_email`` matches a **verified** ``webui_users`` row, the
       subscription is claimed immediately and the buyer's access is provisioned
       (``provision_or_upgrade``).  Otherwise the row stays active + unclaimed,
       to be claimed on the buyer's next verified login.

    The subscription's claimed/api-key/tenant links are written by
    ``provision_or_upgrade``; this function never touches ``api_keys`` directly.
    """
    _enforce_team_min_seats(grant.plan_id, grant.seats)

    subs = subscription_store()
    sub_id = subs.upsert_by_external_ref(
        external_ref=grant.external_ref,
        plan_id=grant.plan_id,
        source=grant.source,
        status="active",
        seats=grant.seats,
        buyer_email=grant.buyer_email,
        amount_cents=grant.amount_cents,
        currency=grant.currency,
        billing_interval=grant.billing_interval,
        current_period_start=grant.current_period_start,
        current_period_end=grant.current_period_end,
        trial_ends_at=grant.trial_ends_at,
    )

    # Claim-at-grant: if the buyer already has a verified account, provision now.
    # provision_or_upgrade owns the claim: it links api_key FIRST then
    # claimed_user_id LAST (I7), so we do NOT pre-link the user here — a failure
    # mid-provision must leave claimed_user_id NULL so the sub stays retryable.
    if grant.buyer_email:
        user_id = _verified_user_id_for_email(grant.buyer_email)
        if user_id is not None:
            provisioning.provision_or_upgrade(sub_id, user_id)
        else:
            logger.info(
                "grant_entitlement: sub_id=%d active but unclaimed "
                "(no verified user for buyer_email); awaits claim-on-login",
                sub_id,
            )

    return sub_id


def update_entitlement(
    external_ref: str,
    *,
    plan_id: int | None = None,
    status: str | None = None,
    current_period_end=None,
    seats: int | None = None,
) -> int:
    """Update commercial fields of an existing entitlement. Returns subscription_id.

    Only the fields passed (non-None) are written.  If ``plan_id`` changes AND the
    subscription is already claimed (``api_key_id`` set), the linked key's plan is
    re-pointed via ``set_api_key_plan_and_overrides`` and the middleware plan
    cache is flushed — **unless** the key's current plan OUTRANKS the new plan
    (highest-tier-wins, ADR-0041 D5): an ``unlimited``-granted key, or a key on a
    pricier plan, is never silently downgraded by a Polar ``subscription.updated``
    event.  The subscription's own ``plan_id`` snapshot still reflects what the
    vendor sent; only the live key is protected.  Limits are NEVER copied — they
    re-resolve via ``plan_id``.

    Raises ``LookupError`` if no subscription exists for ``external_ref``.
    """
    subs = subscription_store()
    sub = subs.get_by_external_ref(external_ref)
    if sub is None:
        raise LookupError(f"update_entitlement: no subscription for external_ref={external_ref!r}")
    sub_id: int = sub["id"]

    updates: dict[str, object] = {}
    if plan_id is not None:
        updates["plan_id"] = plan_id
    if status is not None:
        updates["status"] = status
    if current_period_end is not None:
        updates["current_period_end"] = current_period_end
    if seats is not None:
        updates["seats"] = seats
    if updates:
        subs.update_fields(sub_id, updates)

    # Plan change on a claimed sub → re-point the live key + flush cache, but
    # never DOWNGRADE a higher-tier key (highest-tier-wins, I4).
    plan_changed = plan_id is not None and plan_id != sub["plan_id"]
    key_id = sub["api_key_id"]
    if plan_changed and key_id is not None:
        cur_key_plan_id = _key_plan_id(key_id)
        if cur_key_plan_id is not None and provisioning._plan_outranks(cur_key_plan_id, plan_id):
            logger.info(
                "update_entitlement: sub_id=%d plan change to plan_id=%d ignored for "
                "key_id=%d — current key plan outranks it (no downgrade)",
                sub_id, plan_id, key_id,
            )
        else:
            set_api_key_plan_and_overrides(
                get_pool(), key_id, plan_id, None, None,
                update_rate_limit_override=False,
                update_quota_override=False,
            )
            provisioning._invalidate_plan_cache(key_id)

    return sub_id


def revoke_entitlement(
    external_ref: str, *, reason: str = "cancelled", voluntary: bool = False
) -> None:
    """Revoke an entitlement.

    Two modes:

    * ``voluntary=True`` (user-initiated in-app cancel) → SCHEDULE a
      cancel-at-period-end: ``schedule_cancellation`` sets
      ``cancel_at_period_end=TRUE`` and ``cancelled_at=now()`` but leaves
      ``status='active'`` and does NOT downgrade the key.  Access continues
      until ``current_period_end``; the actual downgrade is driven later by the
      Polar ``subscription.canceled`` webhook firing at real period end, which
      calls this function again with ``voluntary=False``.  This is the no-refund,
      keep-access-to-period-end policy (owner decision #1).

    * ``voluntary=False`` (default — payment failure / refund / admin / the
      period-end webhook) → IMMEDIATE downgrade:

      1. ``mark_cancelled`` → status='cancelled', cancelled_at=now().
      2. If the subscription is claimed (``api_key_id`` set):
       - free plan present → downgrade that key to ``free`` and CLEAR any per-key
         overrides (the paid grant gave them; they go away with the grant), then
         flush the plan cache.  The key stays ``active=TRUE`` — the buyer keeps a
         working free-tier key, they just lose the paid limits.
       - free plan MISSING (should never happen) → FAIL-SAFE: DEACTIVATE the key
         (``active=FALSE``) and log CRITICAL.  A cancellation MUST always stop
         paid access; we never leave a key on the paid plan after a revoke (I12).

    No-op-safe if ``external_ref`` is unknown (logs + returns).
    """
    subs = subscription_store()
    sub = subs.get_by_external_ref(external_ref)
    if sub is None:
        logger.warning(
            "revoke_entitlement: no subscription for external_ref=%r (reason=%r); no-op",
            external_ref, reason,
        )
        return
    sub_id: int = sub["id"]

    if voluntary:
        # User-initiated cancel: schedule cancel-at-period-end and STOP.  The key
        # stays on the paid plan until the Polar period-end webhook downgrades it
        # (that webhook calls this function again with voluntary=False).
        subs.schedule_cancellation(sub_id)
        logger.info(
            "revoke_entitlement: sub_id=%d scheduled cancel-at-period-end "
            "(voluntary, reason=%r); key kept on paid plan until period end",
            sub_id, reason,
        )
        return

    subs.mark_cancelled(sub_id)

    key_id = sub["api_key_id"]
    if key_id is not None:
        free_plan_id = _free_plan_id()
        if free_plan_id is not None:
            set_api_key_plan_and_overrides(
                get_pool(), key_id, free_plan_id, None, None,
                update_rate_limit_override=True,   # clear overrides granted by the paid plan
                update_quota_override=True,
            )
            provisioning._invalidate_plan_cache(key_id)
            logger.info(
                "revoke_entitlement: sub_id=%d cancelled (reason=%r); key_id=%d downgraded to free",
                sub_id, reason, key_id,
            )
        else:
            # Fail-safe: 'free' plan absent → can't downgrade, so kill the key
            # outright rather than leave paid access live after a cancel.
            auth_store().deactivate_api_key(key_id)
            provisioning._invalidate_plan_cache(key_id)
            logger.critical(
                "revoke_entitlement: sub_id=%d cancelled (reason=%r) but 'free' plan "
                "is MISSING — DEACTIVATED key_id=%d as fail-safe to stop paid access",
                sub_id, reason, key_id,
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verified_user_id_for_email(email: str) -> int | None:
    """Return the id of a verified ``webui_users`` row matching ``email``, else None.

    Match is case-insensitive and requires ``email_verified = TRUE`` so a purchase
    can never be claimed onto an unverified (potentially spoofed) account at grant
    time.  Read-only, fully parameterized.
    """
    pool = get_pool()
    with pool.checkout() as conn:
        row = pool.fetch_one(
            conn,
            "SELECT id FROM webui_users "
            "WHERE lower(email) = lower(%s) AND email_verified = TRUE "
            "ORDER BY id LIMIT 1",
            (email,),
        )
    return row["id"] if row is not None else None


def _key_plan_id(key_id: int) -> int | None:
    """Return the plan_id currently assigned to ``key_id``, or None if the key is gone.

    Read-only, fully parameterized.  Used by the update-path highest-tier guard.
    """
    pool = get_pool()
    with pool.checkout() as conn:
        row = pool.fetch_one(
            conn, "SELECT plan_id FROM api_keys WHERE id = %s", (key_id,)
        )
    return row["plan_id"] if row is not None else None


def _free_plan_id() -> int | None:
    """Resolve the downgrade-target free plan id, or None if absent.

    The free slug is admin-configurable via ``billing.free_plan_slug`` (default
    ``'free'``) so a renamed free plan does not silently break revoke.  If the
    configured slug resolves to no row, returns None → revoke fail-safe
    DEACTIVATES the key rather than leaving paid access live (I12).  Fully
    parameterised SELECT.
    """
    from src.settings import get_setting

    free_slug = get_setting("billing.free_plan_slug") or "free"
    pool = get_pool()
    with pool.checkout() as conn:
        row = pool.fetch_one(
            conn, "SELECT id FROM plans WHERE slug = %s", (free_slug,)
        )
    return row["id"] if row is not None else None


def _enforce_team_min_seats(plan_id: int, seats: int) -> None:
    """Reject a Team-tier grant below the configured minimum seat count.

    Single writer (``grant_entitlement``) → this guards the webhook, admin
    Activation, and any future checkout path uniformly.  The team plan slug and
    the minimum are both admin-configurable (``billing.team_plan_slug`` default
    ``'team'``; ``billing.team_min_seats`` default 3).  Raises ``ValueError``
    when the plan IS the team plan AND ``seats`` is below the minimum; the admin
    entitlements route surfaces this as HTTP 422 and the webhook records it in
    the ledger.  A non-team plan, or seats at/above the minimum, is a no-op.
    Read-only, fully parameterised plan-slug lookup.
    """
    from src.settings import get_setting

    team_slug = get_setting("billing.team_plan_slug") or "team"
    min_seats = get_setting("billing.team_min_seats")
    if min_seats is None:
        min_seats = 3

    pool = get_pool()
    with pool.checkout() as conn:
        row = pool.fetch_one(
            conn, "SELECT slug FROM plans WHERE id = %s", (plan_id,)
        )
    plan_slug = row["slug"] if row is not None else None
    if plan_slug == team_slug and seats < min_seats:
        raise ValueError(
            f"team tier requires >= {min_seats} seats (got {seats})"
        )
