# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vendor-neutral billing DB helpers (M10B P1, ADR-0039).

Shared low-level SQL that carries **zero** vendor semantics — both the payment
adapters (Polar, and any future Paddle/ERP adapter) and the admin Activation API
resolve a plan slug through here, so no caller has to import a vendor-named module
just to turn a slug into a ``plans.id``.
"""

from __future__ import annotations

from typing import Any


def slug_to_plan_id(slug: str, conn: Any) -> int:
    """Resolve a plan slug to its integer ``plans.id`` via a parameterised SELECT.

    ``conn`` must be an open psycopg2 connection.  The SELECT is fully
    %s-parameterised (no slug string ever reaches SQL text), so it is safe against
    SQL injection regardless of the slug's origin.

    Args:
        slug: The plan slug to resolve (e.g. ``"pro"``, ``"free"``).
        conn: An open psycopg2 connection.

    Returns:
        Integer ``plans.id`` for the matching plan.

    Raises:
        ValueError: If no plan row exists with the given slug.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s LIMIT 1", (slug,))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"slug_to_plan_id: no plan found with slug={slug!r}")
    return int(row[0])
