# ADR-0041 — Unlimited plan + per-key quota/rpm overrides

**Status:** Accepted
**Date:** 2026-05-28
**Supersedes:** N/A
**Supersedes part of:** ADR-0039 P0 quota gating (extended)
**Context PR:** feat/m10b-p0-rbac-quota-ui (M10B P0-ext)

---

> Relates to [ADR-0039](0039-commercialization-platform.md) (commercialization platform — quota
> gating shipped P0), [ADR-0034](0034-multi-tenant-pooled-isolation.md) (tenant isolation),
> [ADR-0038](0038-tenant-rbac-web-ui-write-side.md) (tenant RBAC web-UI write-side), and
> [ADR-0021](0021-admin-audit-log.md) (audit log taxonomy).

## Context

ADR-0039 P0 shipped quota gating (migration m13_006/m13_007, plan-aware middleware, `/account/usage`
dashboard) via PR #200. However, four operational use cases remained **blocked** because there was
no admin tooling beyond direct psql access:

1. **Grant unlimited access to a key** — e.g. for a pilot customer or internal tooling account.
   The only way was a manual `UPDATE api_keys SET plan_id='...'` against the live DB.
2. **Upgrade a paying user's plan** — free → pro or pro → team transition after a purchase. No
   UI surface existed; upgrades required psql and were invisible in the audit log.
3. **Assign repos/profiles to a tenant via UI** — the admin tenant detail panel had no assignment
   widgets; resource → tenant binding required the CLI.
4. **Reactivate a deactivated API key** — the deactivate flow (added M9) had no mirror endpoint
   or UI surface. Reviving a key required a raw UPDATE.

Additionally, per-key overrides (grandfathering a specific key above its plan's limits, or hard-
capping a key below its plan) were impossible without schema changes.

This ADR records the five decisions that unblock these use cases and extend the P0 quota
architecture.

---

## Decision

### D1 — Implement both: plan `'unlimited'` + per-key override columns

**What:** Seed one special plan row (`slug='unlimited'`, `quota=0`, `rpm=0`, `is_public=FALSE`)
and add two nullable integer columns to `api_keys` (`rate_limit_override INT NULL CHECK >=0`,
`quota_override INT NULL CHECK >=0`) via migration `m13_009`.

**Rationale:** Two distinct needs emerged:

- *Blanket unlimited*: internal accounts, pilot customers, and admin keys should bypass all
  quotas — handled cleanly by a plan whose slug drives SSOT bypass logic in middleware.
- *Fine-grained override*: a paying customer on the Pro plan might need a one-time RPM boost,
  or a free-tier power-user should be hard-capped at a lower quota. Per-key override columns
  serve this without touching the plan definition.

Implementing only one of the two would leave gaps: plan-only cannot express "same plan, different
cap"; per-key-override-only makes "give this key full access" dependent on setting two NULLs to
`0`, which collides with the "zero = zero allowed" semantic (D5).

**Alternatives considered:**
- *Per-plan only*: reject — cannot express per-key exceptions without proliferating plan rows.
- *Per-key override only*: reject — "override 0 = unlimited" is the classic quota confusion trap
  (D5). A dedicated slug is safer and explicit.

### D2 — Tenant = team; no intra-tenant sub-scoping

**What:** Reuse the `tenant_members` M:N table (ADR-0038) as the tenant boundary. No
per-repo or per-profile role distinctions within a tenant are introduced.

**Rationale:** Sub-team scoping within a tenant adds significant complexity (permission matrices,
N-level delegation) for no confirmed customer need at this stage. ADR-0038 already provides the
"one user, many tenants" model for the consultant persona. Within a single tenant, all members
have the same write scope (per ADR-0038 D10).

The admin UI tenant detail panel gains inline repo + profile assignment widgets (W-8), but these
operate at the tenant level, not at a sub-tenant level.

**Deferred:** Per-repo or per-profile ACL within a tenant is a future surface (see §Future work).

### D3 — Per-key PATCH + cascade helper ("Set plan for all keys")

**What:** Two endpoints ship together:
- `PATCH /api/admin/api-keys/{key_id}/plan` — sets plan + optional overrides on a single key.
  `@audit_action('api_key.set_plan')`. Cache-invalidated immediately on the handling worker.
- `PATCH /api/admin/users/{user_id}/plan` — cascades to ALL keys of a user (active + inactive).
  `@audit_action('user.set_plan_cascade')`. One audit log entry per cascade, not per key.

**Rationale:** Upgrading a paying user almost always means upgrading all their keys together.
Requiring N individual PATCH calls creates UX friction and audit log noise. The cascade endpoint
addresses the common case; the single-key endpoint handles exceptions (e.g. a user with one trial
key and one paid key on different plans).

**Alternative considered:** Client-side loop over keys — rejected: produces N audit entries (noisy)
and introduces partial-success state if one key fails.

### D4 — Slug `'unlimited'`, display `'Unlimited (admin-granted)'`, `is_public=FALSE`

**What:** The unlimited plan row has `slug='unlimited'`, a display name of
`'Unlimited (admin-granted)'`, and `is_public=FALSE` so it never appears in the pricing page
tier list or self-serve sign-up flows.

**Rationale:** The slug is the SSOT that drives bypass logic (D5). Making it non-public prevents
a customer from self-selecting it via the Polar.sh checkout flow (M10B P1). The display name
makes its source unambiguous in the admin UI and audit log ("admin granted", not
"purchased plan").

### D5 — Override semantics: CHECK >=0, NULL = plan default, unlimited ONLY via slug

**What:**
- `rate_limit_override` and `quota_override` accept `NULL` (= use plan default) or any integer
  `>=0` (enforced by DB CHECK constraint).
- Override value `0` means **zero is the limit** (a hard cap that blocks all calls). It does NOT
  mean unlimited.
- Unlimited behaviour is **only** activated by the plan slug `'unlimited'`. It is the SSOT.
- Middleware resolves effective limits via `_resolve_effective_rpm` / `_resolve_effective_quota`:
  if plan slug is `'unlimited'` → bypass; else take `override` if non-NULL, else plan default.

**Rationale:** The "0 = unlimited" convention is a well-known source of production incidents in
quota systems (e.g. billing systems that refund a charge by setting it to `0`, accidentally
granting free access). Anchoring unlimited to a named slug eliminates this ambiguity. The
CHECK >=0 constraint ensures no negative value is ever stored (which would have undefined
semantics). Response headers emit `"unlimited"` as the sentinel string when the bypass is active,
making the state observable to the caller.

---

## Implementation

### Migration `m13_009`

```sql
-- Seed unlimited plan (idempotent ON CONFLICT DO NOTHING)
INSERT INTO plans (slug, display_name, quota_calls_per_month, rate_limit_rpm, seat_limit, is_public)
VALUES ('unlimited', 'Unlimited (admin-granted)', 0, 0, 99, FALSE)
ON CONFLICT (slug) DO NOTHING;

-- Per-key override columns (idempotent via information_schema guard + named CHECK)
-- ADD COLUMN IF NOT EXISTS is not used because the inline CHECK is not idempotent across re-runs.
-- The DO block in the actual migration checks information_schema and names the constraint.
ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS rate_limit_override INTEGER,
  ADD COLUMN IF NOT EXISTS quota_override INTEGER;
ALTER TABLE api_keys
  ADD CONSTRAINT api_keys_rate_limit_override_nonneg CHECK (rate_limit_override IS NULL OR rate_limit_override >= 0),
  ADD CONSTRAINT api_keys_quota_override_nonneg      CHECK (quota_override IS NULL OR quota_override >= 0);
```

Both operations are idempotent. Existing keys retain their current plan assignment; nothing is
auto-migrated to unlimited.

### Middleware helpers

`src/mcp/middleware.py` gains two helpers (both accept a single `PlanInfo` argument — override
values are fields on the `PlanInfo` dataclass, not a separate `key_row`):

- `_resolve_effective_rpm(plan: PlanInfo) -> tuple[int, bool]` — returns `(0, True)` (bypass)
  if `plan.slug == 'unlimited'`; else `(plan.rate_limit_override, False)` if non-NULL; else
  `(plan.rate_limit_rpm, False)`.
- `_resolve_effective_quota(plan: PlanInfo) -> tuple[int, bool]` — analogous for monthly quota,
  reading `plan.quota_override` and `plan.quota_calls_per_month`.

The second tuple element `is_unlimited` is `True` only for the unlimited-slug bypass; an explicit
override of `0` returns `(0, False)` (zero-allowed, not unlimited). `RPM=0` on the unlimited plan
hits the `slug == 'unlimited'` branch first, never the `rpm == 0` branch. This maintains the
invariant that `0` in the numeric field means zero-allowed.

### Endpoints (4 new)

| Method | Path | Description | Audit action |
|--------|------|-------------|--------------|
| `PATCH` | `/api/admin/api-keys/{key_id}/plan` | Set plan + overrides on one key | `api_key.set_plan` |
| `PATCH` | `/api/admin/users/{user_id}/plan` | Cascade plan to all keys of user | `user.set_plan_cascade` |
| `POST` | `/api/api-keys/{key_id}/reactivate` | Reactivate deactivated key | `api_key.reactivate` |
| `GET` | `/api/admin/plans` | List all plans incl. `is_public=FALSE` | (read-only) |

`POST /api/api-keys/{key_id}/reactivate`: admin can reactivate any key unconditionally; an
authenticated non-admin may reactivate their own key only (owner-guarded).

### UI sections (4)

- **`/admin/api-keys`** — Plan column with inline plan dropdown + "Overrides..." modal (React
  island, W-5) + Reactivate button in inactive-keys table.
- **`/admin/users`** — "Set plan for all keys" cascade helper per user row (W-6).
- **`/account/api-keys`** — Reactivate button on inactive keys in the self-service account page
  (W-7).
- **`/admin/tenants`** (detail panel) — Inline repo + profile assignment widget (W-8).

---

## Trade-offs

### Known limitations at merge time

- **W-5 limitation — Plan dropdown pre-selection blank on page load:** `GET /api/api-keys` does
  not yet return `plan_id` + override columns in its response. The Overrides modal opens correctly
  (saving works), but the plan dropdown cannot pre-select the key's current plan from the API
  response alone. Deferred to a follow-up: extend `list_api_keys()` to expose `plan_id` +
  overrides + Astro `Key` type update.

- **W-7 limitation — Legacy CTA copy in `_usage-island.tsx` untouched:** The usage island React
  component's "Upgrade your plan" CTA copy was in the forbidden write zone for W-7 (disjoint file
  ownership). W-7 adds a new upgrade hint in the `usage.astro` shell. M10B P1 will consolidate
  both surfaces when the Polar self-serve flow ships.

- **W-8 limitation — `GET /api/repos/profiles` returns `profile.tenant_id`, not `repo.tenant_id`:**
  Currently acceptable because repos inherit `tenant_id` from their profile per ADR-0034.
  Flagged for review when per-repo tenant assignment diverges from profile assignment.

### Operational

- **`_PLAN_CACHE` cross-worker propagation (300s TTL):** The in-memory plan cache in
  `src/mcp/middleware.py` is per-process. A `PATCH /api/admin/api-keys/{id}/plan` request
  invalidates the cache only on the worker that handled the PATCH. Other gunicorn/uvicorn workers
  converge after `_CACHE_TTL` (300 seconds, 5 minutes). To force immediate convergence across all
  workers, restart the workers or wait out the TTL. This is documented in the `post-pr-ops.md`
  runbook (§"Plan changes" section). Redis/PG-NOTIFY-based cross-worker invalidation is deferred
  to M14+.

### Deferred to M10B P1

- Polar.sh webhook integration → Entitlement Activation API (ADR-0039 D5).
- `subscriptions` table + buyer ≠ user split (ADR-0039 §P2).
- Self-serve plan upgrade gated by payment (ADR-0039 D6).

---

## Migration & rollout

- **Migration `m13_009`**: idempotent `ON CONFLICT DO NOTHING` for the unlimited plan seed +
  `IF NOT EXISTS` guards on the two override columns. Safe to run against a live database.
- **Existing keys**: no existing key is auto-assigned to the unlimited plan. Existing plan
  assignments and quotas are preserved.
- **Tests**: existing tests (`test_middleware_quota.py`, `test_m13_006_migration.py`) stay green.
  New tests cover: `m13_009` migration idempotency + constraint checks; middleware
  `_resolve_effective_*` with slug bypass + override precedence; 4 new endpoints; UI reactivate
  button presence.

---

## Operator guidance

**When to use plan `'unlimited'` vs override columns:**

- *Plan 'unlimited'*: for internal admin keys, pilot customers, and accounts that should never
  be gated. Assign via Admin → /admin/api-keys → Plan dropdown → "Unlimited (admin-granted)".
- *Override columns*: for fine-grained adjustments within a plan — e.g., temporarily double a
  Pro key's RPM for a high-volume integration test, or hard-cap a specific free-tier key.
  Set via Admin → /admin/api-keys → "Overrides..." button.

**Upgrading a paying user:** Admin → /admin/users → find user row → pick new plan in dropdown →
"Apply to all keys". Alternatively, upgrade individual keys via /admin/api-keys if the user has
keys on different intended plans.

See `docs/deploy/runbooks/post-pr-ops.md §Plan changes` for step-by-step admin workflows +
cache invalidation sanity + audit log verification.

---

## Future work

- Extend `GET /api/api-keys` response to include `plan_id` + `rate_limit_override` +
  `quota_override` for UI Plan dropdown pre-selection (small follow-up; identified as W-5 gap).
- Wire `tenant_admin` role behavior beyond schema (currently schema-only in `tenant_members.role`).
- Sub-team intra-tenant per-repo / per-profile ACL (deferred, no confirmed need at current scale).
- M10B P1: Polar.sh webhook + Entitlement Activation API + `subscriptions` table.
- Redis / PG-NOTIFY cross-worker plan cache invalidation (M14+ when worker count warrants it).

---

## Amendment — 2026-05-29

**Free-plan consolidation and auto-onboarding (fix/auth-ux-oauth-cache-plans):** The
`free-grandfathered` plan is now deprecated. Migration `m13_013_consolidate_free_plans.sql` repoints
all 6 `free-grandfathered` keys (internal/admin/CLI) to the `unlimited` plan per D5 SSOT, then deletes
the plan row. New signups continue to land on the public `free` plan (100 calls/month, 30 rpm).
Rationale: operational clarity — admin keys should not be bound to a customer-facing free tier; the
deleted row eliminates schema artifact. The unlimited plan (D4) and per-key overrides (D1) are
unaffected.

**Auto-onboarding via auto-minted free-plan API key:** Signups (password + OAuth) now auto-assign
the `free` plan and auto-generate one API key (`auto_{user_id}_{timestamp}`), eliminating manual
key generation. Post-login role-aware landing (`site/src/lib/auth-landing.ts`) directs users to
`/account/api-keys` (customers/tenant owners) or `/admin/` (admins) to view/copy their key. This
closes a customer-friction gap from the unified auth flow (PR #213).
