# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for subscriptions + billing_webhook_events tables via SubscriptionStore."""
import logging

import psycopg2.extras
from psycopg2 import sql as pgsql
from psycopg2.extras import RealDictCursor

from src.db.pg import PgPool

logger = logging.getLogger(__name__)

# Columns that may be updated via update_fields() / _safe_update_clause().
# external_ref is intentionally NOT in this set — it is the idempotency key.
_ALLOWED_UPDATE_COLS: frozenset[str] = frozenset({
    "status",
    "claimed_user_id",
    "api_key_id",
    "tenant_id",
    "plan_id",
    "seats",
    "amount_cents",
    "currency",
    "billing_interval",
    "current_period_start",
    "current_period_end",
    "trial_ends_at",
    "cancelled_at",
    "buyer_email",
    "cancel_at_period_end",
})


class SubscriptionStore:
    """Encapsulates all SQL operations on subscriptions + billing_webhook_events."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------

    def upsert_by_external_ref(
        self,
        *,
        external_ref: str,
        plan_id: int,
        source: str,
        status: str,
        seats: int = 1,
        buyer_email: str | None = None,
        amount_cents: int | None = None,
        currency: str | None = None,
        billing_interval: str | None = None,
        current_period_start=None,
        current_period_end=None,
        trial_ends_at=None,
        claimed_user_id: int | None = None,
        api_key_id: int | None = None,
        tenant_id: int | None = None,
    ) -> int:
        """INSERT ... ON CONFLICT(external_ref) DO UPDATE -> subscription id. Idempotent.

        On conflict the mutable money/timeline fields are refreshed; the
        idempotency key (external_ref) and immutable provenance columns
        (source) are left intact.

        Optional snapshot columns are COALESCEd against the stored value
        (``COALESCE(EXCLUDED.col, subscriptions.col)``) so a partial follow-up
        event — e.g. a renewal/reactivation for the same ``external_ref`` that
        omits ``buyer_email``/``amount_cents``/``currency``/``billing_interval``
        or the period bounds — NEVER erases known data.  This is load-bearing
        for ``buyer_email``: clobbering it with NULL would (a) break
        claim-on-login matching and (b) violate the ``subscriptions_no_orphan_active``
        CHECK on an active+unclaimed row → IntegrityError → silently dropped
        grant.  ``status``/``plan_id``/``seats`` are authoritative on every event
        and are always overwritten from EXCLUDED.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO subscriptions (
                        external_ref, plan_id, source, status, seats,
                        buyer_email, amount_cents, currency, billing_interval,
                        current_period_start, current_period_end, trial_ends_at,
                        claimed_user_id, api_key_id, tenant_id
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT (source, external_ref) DO UPDATE SET
                        status = EXCLUDED.status,
                        plan_id = EXCLUDED.plan_id,
                        seats = EXCLUDED.seats,
                        amount_cents = COALESCE(
                            EXCLUDED.amount_cents, subscriptions.amount_cents),
                        currency = COALESCE(
                            EXCLUDED.currency, subscriptions.currency),
                        billing_interval = COALESCE(
                            EXCLUDED.billing_interval, subscriptions.billing_interval),
                        current_period_start = COALESCE(
                            EXCLUDED.current_period_start, subscriptions.current_period_start),
                        current_period_end = COALESCE(
                            EXCLUDED.current_period_end, subscriptions.current_period_end),
                        trial_ends_at = COALESCE(
                            EXCLUDED.trial_ends_at, subscriptions.trial_ends_at),
                        buyer_email = COALESCE(
                            EXCLUDED.buyer_email, subscriptions.buyer_email),
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        external_ref, plan_id, source, status, seats,
                        buyer_email, amount_cents, currency, billing_interval,
                        current_period_start, current_period_end, trial_ends_at,
                        claimed_user_id, api_key_id, tenant_id,
                    ),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def get_by_external_ref(self, external_ref: str) -> dict | None:
        """Return subscription dict for the given external_ref, or None."""
        with self._pool.checkout() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE external_ref = %s",
                    (external_ref,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    def get_by_id(self, subscription_id: int) -> dict | None:
        """Return subscription dict for the given id, or None."""
        with self._pool.checkout() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE id = %s",
                    (subscription_id,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    def list_by_user(self, user_id: int) -> list[dict]:
        """Return all subscriptions claimed by the given user_id.

        Each returned dict contains all subscriptions columns plus two plan
        enrichment keys added by a LEFT JOIN to plans:
          - plan_slug  (plans.slug)   — e.g. 'pro', 'team', 'free'
          - plan_name  (plans.display_name) — e.g. 'Pro', 'Team', 'Free'

        These extra keys let the account dashboard show the human-readable plan
        name without a second round-trip.  All existing keys (including the new
        cancel_at_period_end column from m13_015) are preserved unchanged.
        """
        with self._pool.checkout() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT s.*,"
                    "       p.slug        AS plan_slug,"
                    "       p.display_name AS plan_name"
                    " FROM subscriptions s"
                    " LEFT JOIN plans p ON p.id = s.plan_id"
                    " WHERE s.claimed_user_id = %s"
                    " ORDER BY s.created_at DESC",
                    (user_id,),
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def list_by_tenant(self, tenant_id: int) -> list[dict]:
        """Return all subscriptions linked to the given tenant_id."""
        with self._pool.checkout() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE tenant_id = %s"
                    " ORDER BY created_at DESC",
                    (tenant_id,),
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def find_unclaimed_active_by_email(self, email: str) -> list[dict]:
        """Return active subscriptions whose buyer_email matches (case-insensitive)
        and claimed_user_id IS NULL.

        Used by claim-on-login to discover unclaimed paid subscriptions for a
        newly-authenticated user.
        """
        with self._pool.checkout() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions"
                    " WHERE lower(buyer_email) = lower(%s)"
                    "   AND status = 'active'"
                    "   AND claimed_user_id IS NULL"
                    " ORDER BY created_at DESC",
                    (email,),
                )
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def update_fields(
        self, subscription_id: int, updates: dict[str, object]
    ) -> bool:
        """Partial UPDATE for the given subscription_id.

        Only columns in _ALLOWED_UPDATE_COLS may appear in updates.
        Raises ValueError for any unknown column (fail-fast before any DB call).
        Always sets updated_at=now().  Returns True if a row was found and
        updated, False if no row with that id exists.
        """
        unknown = set(updates) - _ALLOWED_UPDATE_COLS
        if unknown:
            raise ValueError(
                f"update_fields: unknown column(s) {sorted(unknown)!r}. "
                f"Allowed: {sorted(_ALLOWED_UPDATE_COLS)!r}"
            )
        if not updates:
            raise ValueError("update_fields: updates dict must not be empty")

        stmt, params = self._safe_update_clause(updates, subscription_id)
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(stmt, params)
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def link_to_user(self, subscription_id: int, user_id: int) -> None:
        """Set claimed_user_id on the given subscription."""
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET claimed_user_id = %s, updated_at = now()"
                    " WHERE id = %s",
                    (user_id, subscription_id),
                )
            conn.commit()

    def link_to_api_key(self, subscription_id: int, api_key_id: int) -> None:
        """Set api_key_id on the given subscription."""
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET api_key_id = %s, updated_at = now()"
                    " WHERE id = %s",
                    (api_key_id, subscription_id),
                )
            conn.commit()

    def link_to_tenant(self, subscription_id: int, tenant_id: int) -> None:
        """Set tenant_id on the given subscription."""
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET tenant_id = %s, updated_at = now()"
                    " WHERE id = %s",
                    (tenant_id, subscription_id),
                )
            conn.commit()

    def mark_cancelled(self, subscription_id: int) -> None:
        """Set status='cancelled' and cancelled_at=now()."""
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions"
                    " SET status = 'cancelled', cancelled_at = now(), updated_at = now()"
                    " WHERE id = %s",
                    (subscription_id,),
                )
            conn.commit()

    def schedule_cancellation(self, subscription_id: int) -> None:
        """Schedule a voluntary cancel-at-period-end.

        Sets cancel_at_period_end=TRUE and records cancelled_at=now() as the
        timestamp the user requested cancellation.  Status intentionally stays
        'active' — access continues until current_period_end.

        The actual downgrade (to free plan) is driven by the Polar webhook
        firing subscription.canceled at real period end, which calls
        revoke_entitlement(voluntary=False).  This method is the local state
        signal only (UI feedback + in-app API response).

        Contrast with mark_cancelled(): that method sets status='cancelled'
        immediately (involuntary: payment failure / refund / admin action).
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions"
                    " SET cancel_at_period_end = TRUE,"
                    "     cancelled_at = now(),"
                    "     updated_at = now()"
                    " WHERE id = %s",
                    (subscription_id,),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # billing_webhook_events ledger
    # ------------------------------------------------------------------

    def record_webhook_event(
        self,
        *,
        vendor: str,
        event_id: str,
        event_type: str,
        signature_valid: bool,
        payload: dict,
    ) -> tuple[int | None, bool]:
        """INSERT ... ON CONFLICT(vendor, event_id) DO NOTHING RETURNING id.

        Returns (event_row_id, is_new).
        - is_new=True  → first time we see this (vendor, event_id) pair.
        - is_new=False → duplicate / replay; the row already existed and was
                         not inserted. event_row_id is the id of the existing row.
        """
        with self._pool.checkout() as conn:
            # Attempt the insert; DO NOTHING silently on duplicate.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_webhook_events"
                    " (vendor, event_id, event_type, signature_valid, payload)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (vendor, event_id) DO NOTHING"
                    " RETURNING id",
                    (
                        vendor,
                        event_id,
                        event_type,
                        signature_valid,
                        psycopg2.extras.Json(payload),
                    ),
                )
                row = cur.fetchone()
            conn.commit()

            if row is not None:
                return row[0], True

            # Duplicate — fetch the existing row id.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM billing_webhook_events"
                    " WHERE vendor = %s AND event_id = %s",
                    (vendor, event_id),
                )
                existing = cur.fetchone()
            return (existing[0] if existing else None), False

    def mark_event_processed(
        self,
        event_pk: int,
        subscription_id: int | None,
        error: str | None = None,
    ) -> None:
        """SET processed_at=now(), subscription_id, processing_error."""
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE billing_webhook_events"
                    " SET processed_at = now(), subscription_id = %s,"
                    "     processing_error = %s"
                    " WHERE id = %s",
                    (subscription_id, error, event_pk),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_update_clause(
        updates: dict[str, object], id_val: int
    ) -> tuple:
        """Build (composed_sql, params) for a partial UPDATE of subscriptions.

        Only columns in _ALLOWED_UPDATE_COLS may appear. Unknown keys are
        rejected by the caller (update_fields) before this helper is reached;
        _safe_update_clause itself also silently filters for belt-and-suspenders
        safety.  The frozenset gate makes column-name safety structural:
        sql.Identifier quotes each name, preventing any injection even if the
        frozenset were somehow circumvented.

        Always appends id_val as the final %s parameter for the WHERE clause.
        """
        safe = {k: v for k, v in updates.items() if k in _ALLOWED_UPDATE_COLS}
        if not safe:
            raise ValueError("_safe_update_clause: no valid columns to update")

        stmt = pgsql.SQL(
            "UPDATE subscriptions SET {fields}, updated_at = now()"
            " WHERE id = %s RETURNING id"
        ).format(
            fields=pgsql.SQL(", ").join(
                pgsql.SQL("{col} = %s").format(col=pgsql.Identifier(c))
                for c in safe
            )
        )
        return stmt, list(safe.values()) + [id_val]
