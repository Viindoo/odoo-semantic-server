# SPDX-License-Identifier: AGPL-3.0-or-later
"""EE Modules guard catalogue.

Static fallback list (``_FALLBACK_EE_MODULES``) mirrors the m13_011 INSERT
exactly and remains in code as a safety net when DB is unreachable at startup.

DB-backed ``get_ee_modules()`` reads from the ``ee_modules`` table added by
migration m13_011 and falls back to the static list if the DB is unreachable
or the table is empty.

ADR-0042.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_SOURCE_DATE = "2026-05-08"

# ADR-0055 - why this list has no oracle: the guarded modules are Odoo
# Enterprise (OEEL-1) source, which this repo does not read, so no parser can
# re-derive which Odoo versions ship each name. Where a module IS indexed at a
# version with an edition, that manifest-derived edition is the source of truth
# and check_module_exists does not consult this list; the list only fills the
# gap for names the index does not hold. ``since_version``
# is therefore curated, unverified data: the first Odoo version (``"17.0"``)
# that ships the module as Enterprise, set by an admin in the ee_modules table.
# ``None`` means "not recorded" and the guard applies at every version. The
# static fallback records no ``since_version`` for any row, on purpose - a
# guessed first version would silently drop the warning at older versions.

# ---------------------------------------------------------------------------
# Static fallback — exact mirror of m13_011 INSERT (16 entries).
# KHÔNG xóa khi DB-backed helper hoạt động; là safety net cho startup-without-DB.
# Format: dict with keys matching ee_modules table columns.
# ---------------------------------------------------------------------------

def _ee(name: str, vt: str | None) -> dict[str, Any]:
    """Compact builder for fallback rows — keeps the table within line-length."""
    return {
        "name": name,
        "since_version": None,
        "vt_equivalent": vt,
        "description": None,
        "deprecated": False,
    }


_FALLBACK_EE_MODULES: list[dict[str, Any]] = [
    _ee("knowledge", None),
    _ee("documents", "viin_document"),
    _ee("helpdesk", "viin_helpdesk"),
    _ee("marketing_automation", None),
    _ee("quality", "to_quality"),
    _ee("industry_fsm", None),
    _ee("appointment", "viin_appointment"),
    _ee("planning", None),
    _ee("sign", "viin_sign"),
    _ee("social", "viin_social"),
    _ee("voip", None),
    _ee("whatsapp", None),
    _ee("mrp_plm", "to_mrp_plm"),
    _ee("accountant", "to_account_accountant"),
    _ee("web_studio", None),
    _ee("web_enterprise", None),
]

# Backward-compat: existing code imports EE_CONFUSION dict (module_name → vt_equivalent).
# Computed from static fallback; does NOT query DB.  New code should use get_ee_modules().
EE_CONFUSION: dict[str, str | None] = {
    entry["name"]: entry["vt_equivalent"] for entry in _FALLBACK_EE_MODULES
}

# WI-R F-011 invariant: the fallback list size is frozen so that an accidental
# reorder / de-dupe / append that drops a key surfaces at import time rather
# than as a silent miss in MCP's check_module_exists.  Bump this constant in
# the same PR that adds/removes a fallback row.
_EXPECTED_FALLBACK_COUNT = 16
assert len(_FALLBACK_EE_MODULES) == _EXPECTED_FALLBACK_COUNT, (
    f"_FALLBACK_EE_MODULES drift: expected {_EXPECTED_FALLBACK_COUNT}, "
    f"got {len(_FALLBACK_EE_MODULES)}. Update _EXPECTED_FALLBACK_COUNT in the "
    "same commit that adds/removes a fallback row."
)
assert len(EE_CONFUSION) == _EXPECTED_FALLBACK_COUNT, (
    f"EE_CONFUSION drift: dict size {len(EE_CONFUSION)} != "
    f"{_EXPECTED_FALLBACK_COUNT}.  Likely cause: duplicate `name` in the "
    "fallback list — dict comprehension silently de-duplicates."
)

# ---------------------------------------------------------------------------
# Module-level cache (60 s TTL — same pattern as src/mcp/middleware.py).
# ---------------------------------------------------------------------------

_cache: tuple[list[dict[str, Any]], float] | None = None
_CACHE_TTL = 60.0


def get_ee_modules(conn=None, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return EE Module list, preferring DB over static fallback.

    Caches result 60 s in-process. Use ``force_refresh=True`` after an admin
    write so the next call sees the updated rows immediately.

    Args:
        conn: Optional existing psycopg2 connection.  If *None* (default) a
              temporary connection is opened via ``src.db.pg.get_pool()``
              and closed automatically.
        force_refresh: Bypass the in-process cache and always query the DB.

    Returns:
        List of dicts with keys: name, since_version, vt_equivalent,
        description, deprecated.  Falls back to ``_FALLBACK_EE_MODULES``
        when the DB is unreachable or the table contains no active rows.
    """
    global _cache
    now = time.monotonic()
    if not force_refresh and _cache is not None:
        rows, expiry = _cache
        if now < expiry:
            return rows

    rows = _fetch_from_db(conn)
    if not rows:
        log.debug("ee_modules: DB unreachable or empty, using static fallback")
        rows = list(_FALLBACK_EE_MODULES)

    _cache = (rows, now + _CACHE_TTL)
    return rows


def _version_key(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in version.split("."))
    except (AttributeError, ValueError):
        return None


def ee_guard_applies(entry: dict[str, Any], odoo_version: str | None) -> bool:
    """True when guard row *entry* covers *odoo_version*.

    A row applies from its ``since_version`` onward (numeric compare, so
    ``9.0 < 17.0``). A row without ``since_version``, or a version pair that is
    not numeric, applies at every version - an unknown window keeps the warning
    rather than hiding it.
    """
    since = _version_key(entry.get("since_version") or "")
    current = _version_key(odoo_version or "")
    if since is None or current is None:
        return True
    return current >= since


def invalidate_ee_modules_cache() -> None:
    """Invalidate the in-process cache.  Call after any admin CRUD write."""
    global _cache
    _cache = None


def _fetch_from_db(conn) -> list[dict[str, Any]] | None:
    """Query ee_modules table.  Returns list of dicts, or None on error.

    WI-R F-006 fix: when no caller-supplied connection is provided, use
    the wrapped ``pool.checkout()`` context manager (idiomatic across
    the codebase) instead of reaching into the private
    ``pool._pool.getconn()`` / ``putconn()`` API.  This inherits the
    pool's exception-safe lifecycle (auto-release on raise) and
    decouples this module from psycopg2's specific connection-pool
    implementation.
    """
    if conn is not None:
        return _query_ee_modules(conn)

    try:
        from src.db.pg import get_pool  # noqa: PLC0415
        pool = get_pool()
    except Exception as exc:
        log.debug("ee_modules: cannot acquire DB pool: %s", exc)
        return None

    try:
        with pool.checkout() as pooled_conn:
            return _query_ee_modules(pooled_conn)
    except Exception as exc:
        log.warning("ee_modules: DB query failed: %s", exc)
        return None


def _query_ee_modules(conn) -> list[dict[str, Any]] | None:
    """Run the ee_modules SELECT against a caller-managed connection."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, since_version, vt_equivalent, description, deprecated "
                "FROM ee_modules WHERE deprecated = FALSE ORDER BY name"
            )
            return [
                {
                    "name": r[0],
                    "since_version": r[1],
                    "vt_equivalent": r[2],
                    "description": r[3],
                    "deprecated": r[4],
                }
                for r in cur.fetchall()
            ]
    except Exception as exc:
        log.warning("ee_modules: DB query failed: %s", exc)
        return None
