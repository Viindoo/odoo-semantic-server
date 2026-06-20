# SPDX-License-Identifier: AGPL-3.0-or-later
"""SETTINGS_CATALOGUE — 29 admin-tunable settings.

15 original Tier-1 entries (ADR-0042) + 1 auth entry (ADR-0043: auth.mfa_freshness_seconds)
+ 11 billing.* entries added in M10B P1 (ADR-0039) + 1 support.* entry (helpdesk URL, PR #223)
+ 1 analytics.* entry (GA4 measurement ID, PR #225).

Each entry registers default + validation + metadata. Bootstrap inserts rows
into app_settings table on process start (idempotent ON CONFLICT DO NOTHING).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingDef:
    key: str
    category: str
    data_type: str  # int / float / str / bool / duration_seconds / list_str / struct
    default_value: Any
    validation: dict[str, Any] = field(default_factory=dict)
    requires_restart: bool = False
    requires_reseed: bool = False
    is_secret: bool = False
    description: str = ""
    tenant_scopable: bool = False  # if True, scope='tenant' rows allowed
    # WI-RV F-C: ``advisory`` flags a key whose canonical source-of-truth lives
    # in a DIFFERENT table than ``app_settings`` (e.g. ``quota.*`` is owned by
    # the ``plans`` table — see ADR-0039 / M10B P0).  Admin PATCH still
    # succeeds (the overlay row is updated and the cache is flushed), but
    # the value MUST NOT be read for runtime gating decisions; the live
    # value comes from the canonical table.  The admin UI surfaces this so
    # operators understand the field is a template/default, not a live
    # control.  The two semantics share the catalogue rather than splitting
    # to keep tenant overrides (``tenant_scopable=True``) discoverable.
    advisory: bool = False
    advisory_canonical_source: str = ""  # human-readable pointer (e.g. "table `plans`")


SETTINGS_CATALOGUE: list[SettingDef] = [
    # auth category
    SettingDef("signup.enabled", "auth", "bool", False, {},
               description="Public signup gate. False = invite-only."),
    SettingDef("auth.session_ttl_seconds", "auth", "duration_seconds", 28800,
               {"min": 900, "max": 604800},
               description="Web UI session cookie TTL. 8h default."),
    SettingDef("auth.mfa_grace_period_days", "auth", "int", 7,
               {"min": 0, "max": 30},
               description="Days admin can defer MFA setup."),
    SettingDef("auth.password_min_length", "auth", "int", 12,
               {"min": 8, "max": 64},
               description="Min password length on register/reset."),
    SettingDef("auth.email_verification_ttl_hours", "auth", "int", 24,
               {"min": 1, "max": 168},
               description="Email verification token validity."),
    SettingDef("auth.mfa_freshness_seconds", "auth", "duration_seconds", 300,
               {"min": 60, "max": 3600},
               description=(
                   "Fresh-MFA re-verify window for destructive admin ops"
                   " (restore, settings, plans, EE-modules, patterns)."
               )),

    # quota category — tenant-scopable.  WI-RV F-C: marked ``advisory`` because
    # the live MCP middleware reads quota + rpm from the ``plans`` table
    # (ADR-0039 / M10B P0), NOT from app_settings.  An admin PATCH here
    # updates the catalogue overlay (useful as a plan-tier template default
    # and as a documented tenant override surface) but DOES NOT alter
    # runtime gating until the value is propagated into ``plans``.  The UI
    # surfaces this distinction so operators do not believe they are
    # tuning live quotas through the wrong endpoint.
    SettingDef("quota.free_calls_per_month", "quota", "int", 100,
               {"min": 1, "max": 1_000_000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='free')",
               description="Free tier monthly call quota."),
    SettingDef("quota.free_rpm", "quota", "int", 30,
               {"min": 1, "max": 10000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='free')",
               description="Free tier rate limit (requests/minute)."),
    SettingDef("quota.pro_calls_per_month", "quota", "int", 10000,
               {"min": 1, "max": 10_000_000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='pro')",
               description="Pro tier monthly call quota."),
    SettingDef("quota.pro_rpm", "quota", "int", 120,
               {"min": 1, "max": 10000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='pro')",
               description="Pro tier rate limit."),
    SettingDef("quota.team_calls_per_month", "quota", "int", 100000,
               {"min": 1, "max": 100_000_000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='team')",
               description="Team tier monthly call quota."),
    SettingDef("quota.team_rpm", "quota", "int", 300,
               {"min": 1, "max": 10000}, tenant_scopable=True,
               advisory=True, advisory_canonical_source="table `plans` (slug='team')",
               description="Team tier rate limit."),

    # embedding category
    SettingDef("embedding.max_batch_size", "embedding", "int", 50,
               {"min": 10, "max": 200},
               description="Embedder API batch size."),
    SettingDef("embedding.timeout_read_seconds", "embedding", "duration_seconds", 1200,
               {"min": 300, "max": 7200},
               description="Embedder API read timeout."),

    # indexer category
    SettingDef("indexer.git_clone_timeout_seconds", "indexer", "duration_seconds", 3600,
               {"min": 600, "max": 14400},
               description="Git clone subprocess timeout."),

    # mcp category
    SettingDef("mcp.resource_cache_ttl_seconds", "mcp", "duration_seconds", 300,
               {"min": 30, "max": 3600},
               # The MCP resource ``_CACHE`` singleton is frozen at MCP process
               # start; the admin-settings PATCH runs in the SEPARATE webui
               # process, so a new TTL cannot reach the live MCP cache without
               # an MCP restart (cross-process live-invalidation is out of
               # scope — see fix/startup-reseed-log-noise). Surface the honest
               # "requires MCP restart" hint instead of a misleading
               # propagation ETA.
               requires_restart=True,
               description="MCP odoo:// resource cache TTL."),

    # billing category (M10B P1 — ADR-0039)
    SettingDef(
        "billing.polar_product_map", "billing", "struct", {},
        description=(
            "JSON object mapping Polar product_id → plan slug "
            "(e.g. {\"prod_abc\": \"pro\", \"prod_xyz\": \"team\"}). "
            "Admin-editable; hot-reload ≤60s via get_setting() L1 TTL."
        ),
    ),
    SettingDef(
        "billing.webhook_tolerance_seconds", "billing", "int", 300,
        {"min": 60, "max": 3600},
        description=(
            "Standard-Webhooks timestamp tolerance window (seconds). "
            "Webhooks with webhook-timestamp outside ±tolerance are rejected."
        ),
    ),
    SettingDef(
        "billing.webhook_rate_limit_rpm", "billing", "int", 120,
        {"min": 1, "max": 10000},
        description=(
            "Per-IP rate limit for all vendor webhook endpoints "
            "(POST /api/webhooks/*) (requests/minute)."
        ),
    ),
    # M10B P1 W3 — slug/limit configurability + self-service cancel surface.
    # These promote previously-hardcoded billing constants so ops can rename a
    # plan slug, tune the team minimum, or change the portal/checkout URLs
    # WITHOUT a code change + redeploy (ADR-0042 hot-reload ≤60s).
    SettingDef(
        "billing.free_plan_slug", "billing", "str", "free", {},
        description=(
            "Slug of the downgrade-target free plan used on an involuntary "
            "revoke. If this resolves to no plan, revoke fail-safe DEACTIVATES "
            "the key (never leaves paid access live)."
        ),
    ),
    SettingDef(
        "billing.unlimited_sentinel_slug", "billing", "str", "unlimited", {},
        description=(
            "Slug treated as the top-tier 'never downgrade' sentinel "
            "(ADR-0041 D5). A key on this plan is never downgraded by a Polar "
            "grant/update event."
        ),
    ),
    SettingDef(
        "billing.team_plan_slug", "billing", "str", "team", {},
        description=(
            "Slug of the multi-seat Team plan that the team-min-seats rule "
            "applies to at grant/checkout."
        ),
    ),
    SettingDef(
        "billing.team_min_seats", "billing", "int", 3,
        {"min": 1, "max": 1000},
        description=(
            "Minimum seats enforced on the Team tier at grant/checkout. A grant "
            "for the team plan with fewer seats is rejected (admin route → 422)."
        ),
    ),
    SettingDef(
        "billing.polar_portal_url", "billing", "str", "https://polar.sh/", {},
        description=(
            "Polar customer-portal URL surfaced on the account billing page for "
            "self-serve manage/cancel and shown as the fallback link when the "
            "outbound cancel API is unavailable."
        ),
    ),
    SettingDef(
        "billing.polar_api_base", "billing", "str", "https://api.polar.sh", {},
        description=(
            "Polar REST API base URL used by the outbound cancel client "
            "(POST /api/account/subscription/cancel → Polar cancel endpoint)."
        ),
    ),
    SettingDef(
        "billing.paid_checkout_enabled", "billing", "bool", False, {},
        description=(
            "Gates the public paid-checkout CTA on the pricing page until legal "
            "sign-off. False = show waitlist only; frontend reads this."
        ),
    ),
    SettingDef(
        "billing.polar_checkout_url_map", "billing", "struct", {},
        description=(
            "JSON object mapping plan slug → Polar checkout URL "
            "(e.g. {\"pro\": \"https://buy.polar.sh/...\"}) for the pricing-page "
            "CTA. Frontend reads this; empty = no per-tier checkout link."
        ),
    ),

    # support category (WI-1 — pricing UX overhaul)
    SettingDef(
        "support.helpdesk_url", "support", "str",
        "https://viindoo.com/ticket/team/88",
        {},
        description=(
            "Public helpdesk/ticket URL surfaced on UI-facing contact touchpoints "
            "(pricing FAQ/footer, account claim error panel). "
            "Change this when the Viindoo support-team ID or platform changes. "
            "Surfaced live on the pricing page and GET /api/site-config (within the 60s "
            "settings TTL). Static pages (terms/privacy/refund/account) use a build-time "
            "default synced from this value; changing it there needs a site rebuild."
        ),
    ),

    # analytics category (GA4 — data-driven measurement ID)
    SettingDef(
        "analytics.ga_measurement_id", "analytics", "str", "", {},
        description=(
            "Google Analytics 4 measurement ID (e.g. 'G-XXXXXXXX') injected into "
            "public pages via GET /api/site-config. Empty = analytics disabled. "
            "Admin-tunable; data-driven so changing it needs no site rebuild."
        ),
    ),
]


class SettingValidationError(ValueError):
    """Raised by :func:`validate_setting_value` when a payload fails the catalogue contract.

    The HTTP layer converts this into a 422 response.  Kept as a plain
    ``ValueError`` subclass so non-HTTP callers (CLI / migration scripts)
    can catch it without importing fastapi.
    """


def validate_setting_value(sdef: SettingDef, value: object) -> None:
    """Validate *value* against the catalogue contract of *sdef*.

    Raises:
        SettingValidationError: on type mismatch, min/max breach, or
            enum violation.  The error message is safe to surface to the
            admin caller and includes the violating key + reason.

    Single source of truth for type + range + enum validation; both
    ``src/web_ui/routes/admin_settings.py`` and
    ``src/web_ui/routes/tenant_settings.py`` consume this so a future
    schema extension (e.g., ``regex`` validator) is patched once
    (WI-R F-003).
    """
    t = sdef.data_type
    if t in ("int", "duration_seconds") and not isinstance(value, bool) and isinstance(value, int):
        pass  # accept genuine ints (and explicitly reject bool — see next line)
    elif t in ("int", "duration_seconds"):
        # ``bool`` is a subclass of ``int`` in Python; reject it explicitly so
        # ``True`` does not slip through as ``1`` for an int-typed setting.
        raise SettingValidationError(
            f"Expected int for {sdef.key}, got {type(value).__name__}"
        )
    if t == "float" and not isinstance(value, (int, float)):
        raise SettingValidationError(f"Expected float for {sdef.key}")
    if t == "bool" and not isinstance(value, bool):
        raise SettingValidationError(f"Expected bool for {sdef.key}")
    if t == "str" and not isinstance(value, str):
        raise SettingValidationError(f"Expected str for {sdef.key}")
    if t == "list_str" and not (
        isinstance(value, list) and all(isinstance(x, str) for x in value)
    ):
        raise SettingValidationError(f"Expected list[str] for {sdef.key}")
    v = sdef.validation or {}
    if "min" in v and value < v["min"]:
        raise SettingValidationError(f"{sdef.key} below min {v['min']}")
    if "max" in v and value > v["max"]:
        raise SettingValidationError(f"{sdef.key} above max {v['max']}")
    if "enum" in v and value not in v["enum"]:
        raise SettingValidationError(
            f"{sdef.key} not in allowed enum {v['enum']}"
        )


def _env_seed_signup_enabled() -> bool:
    """Read SIGNUP_ENABLED via env-or-INI using the shared bool-coercion from src.config.

    Resolution order: env var SIGNUP_ENABLED wins; if absent/empty, falls back
    to [webui] signup_enabled in odoo-semantic.conf (via from_env_or_ini); if
    still absent, returns False.  String coercion is delegated to
    :func:`src.config.coerce_bool`: "1", "true", "yes" (case-insensitive)
    → True; everything else (including absent/empty) → False.

    This is called ONLY at seed time inside register_settings_idempotent() to
    capture the operator's deploy-time intent on a FRESH install.

    Import src.web_ui.config is intentionally avoided here to prevent a circular
    import (settings_registry <- src.settings <- src.web_ui.config.signup_enabled
    calls get_overlay_only which imports src.settings which imports
    settings_registry).  src.config itself has no such cycle — it only imports
    stdlib + dotenv — so the shared coerce_bool lives there as a safe anchor.
    """
    from src import config as _cfg
    raw = _cfg.from_env_or_ini("SIGNUP_ENABLED", "webui", "signup_enabled")
    return _cfg.coerce_bool(raw)


def register_settings_idempotent(conn) -> int:
    """Insert SETTINGS_CATALOGUE rows into app_settings table.

    ON CONFLICT (key) DO NOTHING — safe to call on every process start.
    Returns number of rows actually inserted (new settings on this run).

    For ``signup.enabled`` specifically, the seed value is derived from the
    ``SIGNUP_ENABLED`` env var at call time instead of using the catalogue
    default (False).  This honours the operator's deploy-time env on a fresh
    install.  Existing deployments (row already present) are never affected
    because ON CONFLICT DO NOTHING skips the INSERT entirely.  Admin PATCH
    at runtime still wins — it overwrites the same row (ADR-0042 honoured).
    """
    inserted = 0
    with conn.cursor() as cur:
        for sdef in SETTINGS_CATALOGUE:
            # For the signup gate: capture operator deploy-time intent from env.
            # default_json always stays catalogue False (reset-to-default = invite-only).
            if sdef.key == "signup.enabled":
                seed_value = _env_seed_signup_enabled()
            else:
                seed_value = sdef.default_value
            value_json = json.dumps({"v": seed_value})
            default_json = json.dumps({"v": sdef.default_value})
            validation_json = json.dumps(sdef.validation)
            cur.execute(
                """
                INSERT INTO app_settings (
                    key, value_json, category, scope, data_type,
                    validation_json, default_value, requires_restart,
                    requires_reseed, is_secret, description
                ) VALUES (%s, %s::jsonb, %s, 'system', %s,
                          %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL DO NOTHING
                """,
                (sdef.key, value_json, sdef.category, sdef.data_type,
                 validation_json, default_json, sdef.requires_restart,
                 sdef.requires_reseed, sdef.is_secret, sdef.description),
            )
            if cur.rowcount > 0:
                inserted += 1
    conn.commit()
    return inserted


def bootstrap_settings_safe() -> None:
    """Process-start hook. Logs + swallows error to not block startup."""
    try:
        from src.db.pg import get_pool
        pool = get_pool()
        with pool.checkout() as conn:
            conn.autocommit = False
            try:
                inserted = register_settings_idempotent(conn)
                log.info(
                    "Settings bootstrap: %d new row(s) inserted (catalogue=%d)",
                    inserted, len(SETTINGS_CATALOGUE),
                )
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        log.warning("Settings bootstrap FAILED; using code defaults: %s", exc)
