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
- 15 Tier-1 scalar settings (auth + embedding + indexer + mcp categories)
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
- 15 Tier-1 settings refactored + 6 quota Tier-1 plus plans CRUD
- Seeded data: EE modules + patterns CRUD

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
