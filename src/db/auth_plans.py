# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan assignment + per-key override helpers (M10B P0-ext, ADR-0041).

Free functions that operate on a bare PgPool (not AuthStore). Extracted from
auth_registry.py. This is their canonical home — import them directly:
`from src.db.auth_plans import get_plan_by_id`. (The transitional re-export via
auth_registry was removed in the consolidation pass; it carried no patch
surface.)
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.pg import PgPool


# ---------------------------------------------------------------------------
# W-3 helpers: plan assignment + per-key overrides
# Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
# ---------------------------------------------------------------------------


def get_plan_by_id(pg_pool: "PgPool", plan_id: int) -> dict | None:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    """Return plans row as dict or None if not found.

    Used by admin routes to validate plan_id before assignment.

    Args:
        pg_pool: PgPool instance (from src.db.pg.get_pool()).
        plan_id: plans.id to look up.

    Returns:
        Dict with keys id, slug, display_name, quota_calls_per_month,
        rate_limit_rpm, seat_limit, is_public — or None if not found.
    """
    with pg_pool.checkout() as conn:
        row = pg_pool.fetch_one(
            conn,
            "SELECT id, slug, display_name, quota_calls_per_month, "
            "rate_limit_rpm, seat_limit, is_public "
            "FROM plans WHERE id = %s",
            (plan_id,),
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "quota_calls_per_month": row["quota_calls_per_month"],
        "rate_limit_rpm": row["rate_limit_rpm"],
        "seat_limit": row["seat_limit"],
        "is_public": bool(row["is_public"]),
    }


def set_api_key_plan_and_overrides(
    pg_pool: "PgPool",
    key_id: int,
    plan_id: int,
    rate_limit_override: int | None,
    quota_override: int | None,
    *,
    update_rate_limit_override: bool = True,
    update_quota_override: bool = True,
) -> dict:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    # BLOCK-1 fix: partial-update flags added so callers can update only the
    # columns that were explicitly present in the request body (model_fields_set).
    """Atomic UPDATE api_keys with partial-update semantics for override columns.

    Fetches the old snapshot before the update so callers can include both old
    and new values in the audit log.

    Args:
        pg_pool: PgPool instance.
        key_id: api_keys.id to update.
        plan_id: plans.id to assign (always updated).
        rate_limit_override: New rate-limit override value (used only when
            update_rate_limit_override=True).
        quota_override: New quota override value (used only when
            update_quota_override=True).
        update_rate_limit_override: When False, the rate_limit_override column
            is excluded from the SET clause and its current DB value is preserved.
            Default True for backward compatibility with callers that always
            intend to write the override column.
        update_quota_override: Same semantics as update_rate_limit_override but
            for the quota_override column.

    Returns:
        Dict with keys: old_plan_id, old_rate_limit_override, old_quota_override,
        new_plan_id, new_rate_limit_override, new_quota_override.
        new_rate_limit_override / new_quota_override reflect what was actually
        written: the supplied value if the flag is True, or the pre-existing DB
        value if the flag is False (preserved).

    Raises:
        KeyError: key_id does not exist in api_keys.
    """
    with pg_pool.checkout() as conn:
        conn.autocommit = False
        try:
            # Fetch current state (row-lock for atomicity)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan_id, rate_limit_override, quota_override "
                    "FROM api_keys WHERE id = %s FOR UPDATE",
                    (key_id,),
                )
                row = cur.fetchone()
            if row is None:
                raise KeyError(f"API key id={key_id} not found")
            old_plan_id = row[0]
            old_rate_limit_override = row[1]
            old_quota_override = row[2]

            # Build dynamic SET clause — only include override columns when the
            # caller explicitly flagged them as present in the request body.
            # plan_id is always updated (it is a required field in every request).
            set_parts = ["plan_id = %s"]
            params: list = [plan_id]

            if update_rate_limit_override:
                set_parts.append("rate_limit_override = %s")
                params.append(rate_limit_override)

            if update_quota_override:
                set_parts.append("quota_override = %s")
                params.append(quota_override)

            params.append(key_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = %s",
                    params,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    # Resolve the effective new values for the audit snapshot:
    # - if a column was updated, report the supplied value;
    # - if preserved, report the old DB value unchanged.
    new_rate_limit_override = (
        rate_limit_override if update_rate_limit_override else old_rate_limit_override
    )
    new_quota_override = (
        quota_override if update_quota_override else old_quota_override
    )

    return {
        "old_plan_id": old_plan_id,
        "old_rate_limit_override": old_rate_limit_override,
        "old_quota_override": old_quota_override,
        "new_plan_id": plan_id,
        "new_rate_limit_override": new_rate_limit_override,
        "new_quota_override": new_quota_override,
    }


def bulk_set_plan_for_user(
    pg_pool: "PgPool",
    user_id: int,
    plan_id: int,
) -> list[int]:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    """UPDATE plan_id on ALL api_keys (active + inactive) for a given user.

    Per D3: cascade covers ALL keys regardless of active status.
    NOTE: does NOT touch per-key overrides (rate_limit_override, quota_override).

    Args:
        pg_pool: PgPool instance.
        user_id: webui_users.id — must exist (caller validates; 404 returned
            by route if user not found).
        plan_id: plans.id to assign to every key.

    Returns:
        List of affected api_keys.id values (for per-key cache invalidation).
        Empty list if the user has no keys (valid — caller returns 200 + count=0).
    """
    with pg_pool.checkout() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET plan_id = %s "
                    "WHERE user_id = %s RETURNING id",
                    (plan_id, user_id),
                )
                rows = cur.fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return [r[0] for r in rows]
