# ADR-0042: Admin Settings Module — Runtime Configuration Without Restart

**Status:** Accepted
**Date:** 2026-05-28
**Authors:** Engineering team (multi-subagent wave orchestration)
**Supersedes:** —
**Related:** ADR-0021 (audit log), ADR-0022 (MFA TOTP), ADR-0026 (RBAC),
  ADR-0029 (sticky session context), ADR-0034 (multi-tenant pooled),
  ADR-0038 (tenant RBAC), ADR-0039 (commercialization platform)

## Context

OSM had 81 HIGH + 53 MED hardcoded values rải rác (xem inventory): plan quota
seeded trong migration `m13_006`, password policy / session TTL hardcoded
trong `src/web_ui/auth.py`, embedding batch size / git clone timeout trong
`src/constants.py`, EE module guard trong `src/data/ee_modules.py`, pattern
catalogue trong `src/data/patterns.json`.

Hệ quả: ops không thể đổi runtime value mà không SSH + edit + redeploy
(hoặc migration cho seed data). Plan quota, signup gate, embedding batch
size, EE module list — tất cả cần ship-velocity ops control.

## Decision

Ship **Admin Settings** module — 1 thanh "Settings" trong admin web UI cho
phép super-admin + tenant_owner tinker:
- 15 Tier-1 scalar settings (auth + quota + embedding + indexer + mcp categories)
- 4 plan tiers (giữ riêng `plans` table per ADR-0039)
- 16 EE Module guard entries (migrate `src/data/ee_modules.py` → `ee_modules` table)
- 115 patterns (migrate `src/data/patterns.json` → `patterns` table)

**Architecture (5 lớp):**

1. **Storage** — `app_settings` (id PK + 3 partial unique indexes for scope x
   tenant) + `app_settings_history` (ADR-0021 audit cross-link).
2. **Resolution** — 3-tier `get_setting(key, tenant_id=...)` in `src/settings.py`:
   L1 in-memory LRU (60s TTL, bounded 5000) → L2 Postgres → L3 code default
   from `SETTINGS_CATALOGUE`. Tenant override > system > default.
3. **Cache invalidation** — TTL polling per-worker (NOT NOTIFY/LISTEN Phase 1).
   Effective window ≤60s; UI surfaces this constraint explicitly.
4. **Endpoints** — 6 admin + 5 tenant + 4 plans + 5 ee_modules + 6 patterns =
   26 new HTTP routes under `/api/admin/*` and `/api/tenants/{tid}/settings/*`.
5. **UI** — Astro SSR + React islands; Viindoo brand (cyan #00BBCE primary,
   purple #7F4282 destructive, Montserrat heading + Roboto body).

**Tenant scope (Phase 1):** Only `quota.*` settings (6 keys) are tenant-scopable.
Other categories defer to Phase 2.

## Consequences

### Positive
- Ops can tune RPM, quota, password policy, embedding batch without redeploy.
- Tenant admins self-service for custom enterprise quota.
- Audit + rollback (undo last-10 + reset-to-default) for every mutation.
- Hot-reload ≤60s for non-restart settings; restart class flagged in UI.

### Negative
- +2 Postgres tables + 3 new tables (app_settings, app_settings_history,
  ee_modules, patterns); +2 migrations (m13_010/010/011).
- Cache TTL semantics: admin sees stale value for ≤60s after another worker
  PATCH. Documented and acceptable.
- Cross-worker invalidation requires NOTIFY/LISTEN if scaling beyond single
  host — deferred to Phase 2.

### Risks (mitigations)
1. Admin set `quota.free_rpm=0` → block all free traffic.
   Mitigation: `validation_json={"min":1}` enforced + ≥50% drop warning.
2. Tenant cache key explosion (1000 tenants × 15 settings = 15k entries).
   Mitigation: LRU bounded 5000 + LFU-ish eviction.
3. Concurrent PATCH same key.
   Mitigation: Postgres row lock + history captures both.
4. Bootstrap fail fresh deploy.
   Mitigation: `bootstrap_settings_safe()` try/except non-blocking; fallback
   to code defaults.

## Phase 1 → Phase 2 Roadmap

**Phase 1 (this ADR, shipped):**
- System scope full
- Tenant scope cho `quota.*` only
- TTL polling cache invalidation
- 15 Tier-1 settings refactored (incl. the 6 quota.* settings) plus plans CRUD
- Seeded data: EE modules + patterns CRUD
- Live SSOT for the settings catalogue: `src/settings_registry.py` (the catalogue has since grown to 18 non-billing Tier-1 / 29 total entries post-ADR-0043 + PR #223/#225; this ADR documents the state at ship time)

**Phase 2 (deferred):**
- Tenant scope cho all categories (multi-tenant policy isolation)
- NOTIFY/LISTEN cache invalidation (multi-host deployment)
- Tier-2 settings: output caps, login rate-limit, MCP tool defaults (~30 more keys)
- CSS-CSP nonce settings
- Bulk file upload for patterns/ee_modules

## Alternatives Considered

**A. Pure env-based config with reload signal**
Reject: requires shell access; doesn't audit; multi-process race; no rollback.

**B. Single giant `config` table with all knobs**
Reject: seeded data (patterns 115 rows × 10 columns) and scalar settings have
different shapes; mixing them fights normalization. Separate tables for plans,
ee_modules, patterns preserves FK + audit granularity.

**C. Feature flag service (LaunchDarkly etc.)**
Reject: external dependency for self-hosted OSM; cost; out of scope for
ops-tunable system config (flags are A/B, settings are policy).

**D. Schema invariants exposed (bcrypt cost, era boundaries, ORM enums)**
Reject: 1 mutation = corrupt graph + reindex; rủi ro xa vượt lợi ích UI.
Code-only.

## File Map

| Component | Files |
|---|---|
| Schema | `migrations/m13_010_app_settings.sql`, `m13_011_ee_modules.sql`, `m13_012_patterns.sql` |
| Resolver | `src/settings.py`, `src/settings_registry.py` |
| Bootstrap | `src/web_ui/app.py` lifespan + `src/mcp/server.py` lifespan |
| Admin routes | `src/web_ui/routes/admin_settings.py`, `admin_plans.py`, `admin_ee_modules.py`, `admin_patterns.py` |
| Tenant routes | `src/web_ui/routes/tenant_settings.py` |
| UI pages | `site/src/pages/admin/settings/*.astro`, `site/src/pages/tenant/settings/*.astro` |
| UI islands | `site/src/pages/admin/settings/_*-island.tsx` (5), `tenant/settings/_*-island.tsx` (1) |
| Tests | `tests/test_settings_resolver.py`, `test_bootstrap_hook.py`, `test_constants_fallback.py`, `test_admin_*.py`, `test_tenant_*.py`, `test_e2e_quota_hotreload.py`, `test_migration_m13_010/010/011.py`, `test_migration_rollback_admin_settings.py` |

---

### Follow-up: osm_reader sequence grant (BUG CLASS A)

`osm_reader` must hold **`USAGE` on `app_settings_id_seq`** in addition to
`INSERT` on `app_settings`. `bootstrap_settings_safe()` UPSERTs catalogue rows
on MCP startup; because `app_settings.id` is `BIGSERIAL`, Postgres evaluates the
`id` default (`nextval('app_settings_id_seq')`) BEFORE `ON CONFLICT DO NOTHING`,
so `INSERT` alone fails with *"permission denied for sequence
app_settings_id_seq"*. General rule for this project's grant set (see also
ADR-0034): **any table `osm_reader` has `INSERT` on AND that has a
serial/identity PK must also get `USAGE` on its backing sequence.** Granting
`INSERT` without the sequence `USAGE` is an incomplete grant. SELECT-only tables
(`app_settings_history`, `ee_modules`, `patterns`) need no sequence `USAGE`. The
grant lives in BOTH `migrations/m13_010_app_settings.sql` and
`ops/rls_create_osm_reader.sql` (SSOT). Discovered during the live ADR-0042
deploy and hotfixed in prod; codified in `fix/admin-settings-grants-dotenv`.

---

### Amendment 2026-05-31 (PR #223 — feat/site-pricing-ux)

- **28th catalogue entry:** `support.helpdesk_url` (`support` category, `str`, default `""`)
  added in `src/settings_registry.py`. When non-empty, the SiteHeader renders a "Help" link.
- **`GET /api/site-config` (public, no auth):** New endpoint that exposes only two settings
  deemed safe for anonymous access: `helpdesk_url` + `site_version`. Registered in
  `src/web_ui/app.py`; exempt from the auth middleware via `src/web_ui/middleware.py` allowlist.
  Policy: only settings whose exposure carries no security or business risk are added here;
  consult this ADR + ADR-0026 before extending the response payload.

---

### Amendment 2026-06-01 (PR #225 — feat/web-integration)

- **29th catalogue entry:** `analytics.ga_measurement_id` (`analytics` category, `str`, default `""`)
  added in `src/settings_registry.py`. Injected into public Astro pages for GA4 + Consent Mode v2.
  Empty string = analytics disabled. Public client-side token; no security or business-confidentiality risk.
- **`GET /api/site-config` response extended (5 fields):** The endpoint now returns five fields
  (previously two). Full contract in `src/web_ui/routes/site_config.py` module docstring:
  ```json
  {
    "helpdesk_url": "https://viindoo.com/ticket/team/88",
    "site_version": "0.13.1",
    "paid_checkout_enabled": false,
    "checkout_url_map": {"<plan_slug>": "<url>"},
    "ga_measurement_id": ""
  }
  ```
  Safety rationale for each new field:
  - `paid_checkout_enabled` — boolean CTA gate; boolean flags carry no sensitive info.
  - `checkout_url_map` — Polar buy-links are intentionally public; returned **only** when
    `paid_checkout_enabled` is `true` (pre-launch the flag is `false`, so the Polar URLs remain
    unexposed). Gating logic in `site_config.py`.
  - `ga_measurement_id` — GA4 measurement ID is a public client token (`G-XXXXXXXX`); its
    purpose is to appear in public page source.
  Policy unchanged: consult this ADR + ADR-0026 before adding any further fields.
- **New `analytics` category** — `/admin/settings/analytics` page in `site/src/pages/admin/settings/`
  (dynamic `[category].astro` + index card). Admin-tunable without redeploy; hot-reload ≤60s.
- **CSP updated** — `connect-src` in `site/src/middleware.ts` and `docs/deploy/nginx-m8.conf`
  extended to allow `https://www.google-analytics.com` (GA4 beacon endpoint).
