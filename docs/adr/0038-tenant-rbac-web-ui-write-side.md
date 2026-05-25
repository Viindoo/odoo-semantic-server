# ADR-0038 — Tenant RBAC web-UI write-side + customer self-service foundation

**Status:** Accepted (W1 — 2026-05-25)
**Extends:** ADR-0026 (RBAC), ADR-0034 (read-side isolation)
**Related:** ADR-0011 (session auth), ADR-0021 (audit), ADR-0029 (implicit session context)

---

## Context

ADR-0026 introduced the admin vs. self-service split (`/admin/*` vs. `/account/*`) and
defined `require_admin` as the gate for all mutating control-plane routes (enforced by
Wave 0, PR #174). ADR-0034 added **read-side** (MCP query) tenant isolation via the
`profile_name = ANY(string_to_array(allowed_profiles, ','))` GUC mechanism.

However, the **web-UI write-side** (creating/managing repos, profiles, API keys) remained
tenant-blind: an admin could see all resources of all tenants simultaneously, and there
was no mechanism to associate a web-UI user login with a specific tenant, making it
impossible to build a customer self-service portal scoped to a single organization.

This ADR defines the write-side authz layer that makes the web-UI tenant-aware.

---

## Problem Statement

1. No link between `webui_users` and `tenants` — no way to know which tenant a web user
   represents when they create or mutate resources.
2. One-user-one-tenant is insufficient for the consultant (agency) persona who manages
   multiple customers; the model must allow one user to belong to many tenants.
3. Active-tenant disambiguation: when a user belongs to N tenants, which tenant does a
   mutating action target? Must be explicit and auditable, not implicit/session-based.
4. Read-side GUC guard gap: `profiles.name` had no constraint preventing a comma in the
   name, which would corrupt the `string_to_array(allowed_profiles, ',')` parsing in
   the RLS read-side isolation (ADR-0034 A4 noted but deferred).
5. Schema drift: `webui_users.password_hash NOT NULL` predated OAuth support; OAuth-only
   users were already inserting NULL in production (#176).

---

## Decisions

### D1 — Membership model (b) `tenant_members`

A new `tenant_members(user_id, tenant_id, role, created_at)` join table links
`webui_users.id` to `tenants.id` in an M:N relationship (one user, many tenants).
`PRIMARY KEY (user_id, tenant_id)`. `ON DELETE CASCADE` on both FKs.

`webui_users.is_admin = TRUE` = global admin; such users bypass tenant-scope entirely
(see D4). Default role = 'member'; future waves may use 'tenant_admin' for
intra-tenant permission delegation without granting global admin access.

Rationale: consultant/agency persona manages multiple customers; SME single-tenant
user is just the degenerate 1-member case.

### D2 — Write-side authz tightly coupled to read-side invariant but implemented separately

The web-UI write-side uses `resolve_tenant_scope_web(request) -> set[int] | ALL_TENANTS`
(in `src/web_ui/auth.py`) to determine which tenants the session user may act within.

The MCP read-side uses `RepoStore.resolve_tenant_scope(tenant_id) -> (own, shared)` to
determine which profile names a given API key may query against.

**Shared invariant**: both tiers enforce the same `profile_name = ANY(allowed)` contract
from ADR-0034 A2/A4. The write-side assigns `repos.tenant_id` / `profiles.tenant_id`;
the read-side reads those same values to build the allowed-profile list.

**Kept separate** to avoid coupling MCP request context (API key + tenant_id) with
web-UI session context (cookie + user_id). Combining them would create a God object
and make both harder to test in isolation.

### D3 — Active-tenant = explicit `tenant_id` in request body (Option A, stateless)

When a user belongs to multiple tenants, each mutating request carries an explicit
`tenant_id` in the request body. The handler validates `tenant_id IN scope` (or admin
bypass) before performing the operation.

**Rationale (ETHOS #3 Keep Simple + #6 fail-safe-by-default):**
- No confused-deputy: each request is self-describing; audit log records the exact
  tenant_id without session inference.
- Stateless: no session sentinel to invalidate when membership is revoked.
- `RepoStore.add_repo(tenant_id=...)` already accepted an optional `tenant_id` (M13).
- Option B (session switcher / "company switcher") deferred: can be built on top of
  Option A without changing the underlying contract.
- Option C (nested URL `/api/tenants/{id}/repos`) rejected: would require refactoring
  all existing route prefixes and break backward compat.

### D4 — Admin bypass is absolute; W0 gates are preserved

`is_admin = TRUE` -> `resolve_tenant_scope_web` returns `ALL_TENANTS` sentinel; every
list is unfiltered and every write skips membership check.

The tenant-scope layer **adds on top of** `require_admin` (W0); it does NOT replace it.
All routes that were admin-only in W0 remain admin-only in W1. Opening routes for
non-admin (tenant-member) access is Wave 2.

### D5 — GUC-delimiter guard: `CHECK (profiles.name NOT LIKE '%,%')`

Added in migration m13_005, Part C. Closes the open debt from ADR-0034 A4: a comma in
a profile name would corrupt `string_to_array(current_setting('app.allowed_profiles'), ',')`
in the PostgreSQL RLS policy, silently granting or denying access to unintended profiles.

The constraint validates at INSERT/UPDATE time. If existing data contains a comma, the
migration will fail loudly (not silently skip) — this is intentional (fail-fast).

### D6 — password_hash nullable (fold #176, Option A)

`webui_users.password_hash` was `NOT NULL` in the repo schema (9000_webui_users.sql)
but the production database had already drifted: OAuth-only users insert `NULL`.

Fix: `ALTER TABLE webui_users ALTER COLUMN password_hash DROP NOT NULL` (idempotent;
no-op if column is already nullable). Folded into m13_005 to avoid an isolated migration.

### D7 — Shared/global resources (`tenant_id IS NULL`) are admin-mutate-only

Resources with `tenant_id IS NULL` (spec data, base Odoo CE profiles, global shared repos)
are readable by all tenants (ADR-0034 D3) but MUST NOT be mutated by non-admin users.
Admin users may assign any resource to or from NULL via `PATCH /api/{profiles,repos}/{id}/tenant`.

### D8 — Delete tenant is blocked when resources remain

`DELETE /api/tenants/{id}` returns 409 if any `repos.tenant_id = id` or
`profiles.tenant_id = id` rows exist. Tenant membership rows (`tenant_members`) do
CASCADE on delete (safe — they are permission grants, not data).

Rationale: silently cascading `repos.tenant_id` to NULL when a tenant is deleted would
change the scope of those repos to "global/shared" without the admin explicitly deciding.
This could expose customer data unintentionally.

---

## Consequences

- **Migration m13_005** must be applied before Wave 2 (opens non-admin write routes).
  Contains 3 parts: tenant_members DDL + password_hash nullable + GUC-delimiter guard.
- **Wave 2 precondition**: before opening non-admin self-service routes, admin must
  assign existing non-admin users to their correct tenants via `admin/tenants` page;
  a non-admin with no membership rows receives `scope = set()` (deny-all for writes).
- `auth.py` now exports `resolve_tenant_scope_web`, `is_in_scope`, `ALL_TENANTS`
  — Wave 2 imports these; they MUST NOT be renamed or signature-changed without
  updating Wave 2 code.
- The `/admin/tenants` Astro page and `routes/tenants.py` FastAPI router are the
  primary management surface for this layer.
- `PATCH /api/profiles/{id}/tenant` MUST call `invalidate_allowed_profiles()` (same
  as create/update/delete profile in repos.py) to prevent the MCP read-side from
  serving stale tenant-scope data after a profile is re-assigned.

---

## Alternatives Considered

**Option B (session active-tenant switcher)**: deferred. Confused-deputy risk (user
forgets which tenant is active, creates repo under wrong customer). Can be layered on
top of Option A later without schema changes.

**Option C (nested URL `/api/tenants/{tid}/repos`)**: rejected. Requires breaking
refactor of all existing route prefixes (W0/W2/W4 disjoint-ownership conflict).

**Model (a) — one-user-one-tenant (`webui_users.tenant_id` column)**: rejected.
Does not support the consultant persona (multiple customers). `tenant_members` is
strictly more expressive.

---

## Files

| File | Change |
|------|--------|
| `migrations/m13_005_tenant_members.sql` | New — 3-part migration |
| `src/db/auth_registry.py` | New methods: tenant CRUD + member management |
| `src/web_ui/auth.py` | New: `ALL_TENANTS`, `resolve_tenant_scope_web`, `is_in_scope` |
| `src/web_ui/routes/tenants.py` | New — admin-only tenant/member/resource routes |
| `src/web_ui/app.py` | Wire tenants.router |
| `src/web_ui/routes/repos.py` | Bug (i): early 404 for missing profile; bug (ii): comment status/clone_status |
| `src/db/repo_registry.py` | Docstring: clarify status vs clone_status in add_repo |
| `site/src/middleware.ts` | requireAdmin gate for `/admin/tenants` |
| `site/src/pages/admin/tenants.astro` | New — admin tenant management page |
| `site/src/pages/admin/_tenants-island.tsx` | New — React island for mutations |
| `tests/test_w1_tenant_rbac.py` | New — 14-case test suite |
