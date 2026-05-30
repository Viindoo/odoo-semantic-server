# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for subscriptions + billing_webhook_events tables via SubscriptionStore."""
import logging

import psycopg2.extras
from psycopg2 import sql as pgsql

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
    # #5 monotonic guard high-water-mark: update_entitlement advances this via the
    # partial-UPDATE path so a stale (out-of-order) event still moves the watermark
    # forward without flipping authoritative status/plan/seats. The activation layer
    # computes the GREATEST in Python (it already holds the stored value); this set
    # only needs to permit the column to be written.
    "last_event_at",
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
        last_event_at=None,
    ) -> int:
        """INSERT ... ON CONFLICT(source, external_ref) DO UPDATE -> subscription id. Idempotent.

        On conflict the mutable money/timeline fields are refreshed; the
        idempotency key (source, external_ref) and immutable provenance columns
        (source) are left intact.

        Optional snapshot columns are COALESCEd against the stored value
        (``COALESCE(EXCLUDED.col, subscriptions.col)``) so a partial follow-up
        event — e.g. a renewal/reactivation for the same ``external_ref`` that
        omits ``buyer_email``/``amount_cents``/``currency``/``billing_interval``
        or the period bounds — NEVER erases known data.  This is load-bearing
        for ``buyer_email``: clobbering it with NULL would (a) break
        claim-on-login matching and (b) violate the ``subscriptions_no_orphan_active``
        CHECK on an active+unclaimed row → IntegrityError → silently dropped
        grant.

        Monotonic guard (#5): ``status``/``plan_id``/``seats`` are authoritative
        but ONLY when the incoming event is at least as recent as the stored one.
        ``last_event_at`` is the vendor event timestamp (Polar ``modified_at`` /
        event ``created_at``).  Webhooks can be delivered out of order, so an
        older ``subscription.updated`` event must NOT overwrite a newer
        ``subscription.canceled`` (or vice-versa).  The CASE WHEN below keeps the
        stored authoritative columns whenever ``EXCLUDED.last_event_at`` is
        strictly older than the stored ``last_event_at``.  When either side's
        timestamp is NULL (caller did not supply one, or the row predates the
        column) the guard degrades to last-write-wins so legacy callers keep
        working.  ``last_event_at`` itself advances via ``GREATEST`` so the
        high-water-mark never regresses.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO subscriptions (
                        external_ref, plan_id, source, status, seats,
                        buyer_email, amount_cents, currency, billing_interval,
                        current_period_start, current_period_end, trial_ends_at,
                        claimed_user_id, api_key_id, tenant_id, last_event_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (source, external_ref) DO UPDATE SET
                        status = CASE
                            WHEN EXCLUDED.last_event_at IS NULL
                              OR subscriptions.last_event_at IS NULL
                              OR EXCLUDED.last_event_at >= subscriptions.last_event_at
                            THEN EXCLUDED.status
                            ELSE subscriptions.status
                        END,
                        plan_id = CASE
                            WHEN EXCLUDED.last_event_at IS NULL
                              OR subscriptions.last_event_at IS NULL
                              OR EXCLUDED.last_event_at >= subscriptions.last_event_at
                            THEN EXCLUDED.plan_id
                            ELSE subscriptions.plan_id
                        END,
                        seats = CASE
                            WHEN EXCLUDED.last_event_at IS NULL
                              OR subscriptions.last_event_at IS NULL
                              OR EXCLUDED.last_event_at >= subscriptions.last_event_at
                            THEN EXCLUDED.seats
                            ELSE subscriptions.seats
                        END,
                        last_event_at = GREATEST(
                            COALESCE(subscriptions.last_event_at, EXCLUDED.last_event_at),
                            COALESCE(EXCLUDED.last_event_at, subscriptions.last_event_at)
                        ),
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
                        claimed_user_id, api_key_id, tenant_id, last_event_at,
                    ),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def get_by_external_ref(self, external_ref: str) -> dict | None:
        """Return subscription dict for the given external_ref, or None."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn,
                "SELECT * FROM subscriptions WHERE external_ref = %s",
                (external_ref,),
            )

    def get_by_id(self, subscription_id: int) -> dict | None:
        """Return subscription dict for the given id, or None."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn,
                "SELECT * FROM subscriptions WHERE id = %s",
                (subscription_id,),
            )

    def list_by_user(self, user_id: int) -> list[dict]:
        """Return all subscriptions claimed by the given user_id.

        Each returned dict contains all subscriptions columns plus two plan
        enrichment keys added by a LEFT JOIN to plans:
          - plan_slug  (plans.slug)   — e.g. 'pro', 'team', 'free'
          - plan_name  (plans.display_name) — e.g. 'Pro', 'Team', 'Free'

        These extra keys let the account dashboard show the human-readable plan
        name without a second round-trip.  All existing keys (including the new
        cancel_at_period_end column (added in m13_014_billing_p1)) are preserved unchanged.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT s.*,"
                "       p.slug        AS plan_slug,"
                "       p.display_name AS plan_name"
                " FROM subscriptions s"
                " LEFT JOIN plans p ON p.id = s.plan_id"
                " WHERE s.claimed_user_id = %s"
                " ORDER BY s.created_at DESC",
                (user_id,),
            )

    def list_by_tenant(self, tenant_id: int) -> list[dict]:
        """Return all subscriptions linked to the given tenant_id."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT * FROM subscriptions WHERE tenant_id = %s"
                " ORDER BY created_at DESC",
                (tenant_id,),
            )

    def list_all(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Return a page of all subscriptions (admin list), newest first.

        Explicit column projection (no SELECT *) + LEFT JOIN plans for the
        ``plan_slug``/``plan_name`` enrichment (mirrors list_by_user) so the
        admin dashboard can render the human-readable plan without a second
        round-trip.  Paginated via LIMIT/OFFSET; ORDER BY created_at DESC with
        an id tiebreak for a deterministic page boundary when several rows
        share the same created_at.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT s.id, s.plan_id, s.claimed_user_id, s.api_key_id,"
                "       s.tenant_id, s.buyer_email, s.status, s.seats,"
                "       s.source, s.external_ref, s.amount_cents, s.currency,"
                "       s.billing_interval, s.current_period_start,"
                "       s.current_period_end, s.trial_ends_at, s.cancelled_at,"
                "       s.cancel_at_period_end, s.last_event_at,"
                "       s.created_at, s.updated_at,"
                "       p.slug         AS plan_slug,"
                "       p.display_name AS plan_name"
                " FROM subscriptions s"
                " LEFT JOIN plans p ON p.id = s.plan_id"
                " ORDER BY s.created_at DESC, s.id DESC"
                " LIMIT %s OFFSET %s",
                (limit, offset),
            )

    def find_unclaimed_active_by_email(self, email: str) -> list[dict]:
        """Return active subscriptions whose buyer_email matches (case-insensitive)
        and claimed_user_id IS NULL.

        Used by claim-on-login to discover unclaimed paid subscriptions for a
        newly-authenticated user.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT * FROM subscriptions"
                " WHERE lower(buyer_email) = lower(%s)"
                "   AND status = 'active'"
                "   AND claimed_user_id IS NULL"
                " ORDER BY created_at DESC",
                (email,),
            )

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

    def claim_unclaimed_for_user(
        self, subscription_id: int, user_id: int
    ) -> bool:
        """Atomically claim an UNCLAIMED subscription for ``user_id`` (CAS).

        This is the race-safe claim path for claim-on-login (WI-3 claim-FIRST).
        The compare-and-set lives entirely in the WHERE clause:

            UPDATE ... SET claimed_user_id = %s
             WHERE id = %s AND claimed_user_id IS NULL

        Postgres takes a row lock on the matching row, so when two requests race
        to claim the same subscription exactly one UPDATE matches a row with
        ``claimed_user_id IS NULL`` and the other sees the already-set value and
        matches zero rows.

        Returns:
            True  — this caller won the claim (rowcount == 1).
            False — already claimed (by this or another user) or no such id;
                    the caller lost the race / the sub was previously claimed.

        Unlike ``link_to_user`` this NEVER re-points an already-claimed
        subscription to a different user — claim is one-way and first-writer-wins.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions"
                    " SET claimed_user_id = %s, updated_at = now()"
                    " WHERE id = %s AND claimed_user_id IS NULL",
                    (user_id, subscription_id),
                )
                claimed = cur.rowcount == 1
            conn.commit()
        return claimed

    def link_to_user(self, subscription_id: int, user_id: int) -> None:
        """Set claimed_user_id on the given subscription (unconditional re-link).

        Used for internal/admin re-linking where overwriting an existing claim is
        intentional.  For the race-safe first-writer-wins claim path used by
        claim-on-login, prefer ``claim_unclaimed_for_user`` which only succeeds
        when ``claimed_user_id`` is still NULL.
        """
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
    ) -> tuple[int, bool, bool]:
        """INSERT ... ON CONFLICT(vendor, event_id) idempotent upsert.

        Returns ``(pk, is_new, already_processed)``:
          - pk                 — id of the ledger row. NEVER None: the
                                 ``DO UPDATE`` no-op below always RETURNS the row
                                 whether it was just inserted or already existed.
          - is_new             — True the first time we see this
                                 (vendor, event_id) pair; False on replay.
          - already_processed  — True iff the existing row already has a
                                 ``processed_at`` timestamp (i.e. a successful
                                 prior run already applied this event).  The
                                 webhook pipeline uses this to short-circuit a
                                 fully-processed replay while still RE-processing
                                 a row that was recorded but never finished
                                 (crash between INSERT and mark_event_processed).

        Why ``ON CONFLICT ... DO UPDATE`` instead of ``DO NOTHING``: a bare
        ``DO NOTHING ... RETURNING`` returns NO row on conflict, forcing a
        second SELECT that can race / return None.  A no-op ``DO UPDATE SET
        received_at = received_at`` is guaranteed to touch the conflicting row
        so ``RETURNING`` always yields exactly one row.  ``(xmax = 0)`` is the
        canonical Postgres trick to tell a fresh INSERT (xmax 0) from a row that
        went through the UPDATE branch (xmax set to the updating xid).
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO billing_webhook_events"
                    " (vendor, event_id, event_type, signature_valid, payload)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (vendor, event_id) DO UPDATE"
                    "   SET received_at = billing_webhook_events.received_at"
                    " RETURNING id, (xmax = 0) AS is_new,"
                    "           (processed_at IS NOT NULL) AS already_processed",
                    (
                        vendor,
                        event_id,
                        event_type,
                        signature_valid,
                        psycopg2.extras.Json(payload),
                    ),
                )
                pk, is_new, already_processed = cur.fetchone()
            conn.commit()
        return pk, is_new, already_processed

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
