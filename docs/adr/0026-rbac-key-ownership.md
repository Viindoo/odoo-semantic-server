# ADR-0026 — RBAC + Key Ownership

**Status:** Accepted  
**Date:** 2026-05-18  
**Milestone:** M9 follow-up

---

## Context

During M9 deployment, a regression emerged: admins logged into the Web UI saw 0 API keys in the
`/admin/api-keys` list. Root cause: `src/web_ui/routes/api_keys.py:57` read
`request.session.get("is_admin", False)`, but the login flow (`src/web_ui/routes/login.py:402-405`)
never wrote `is_admin` to the session cookie. Session fields must be explicitly set by the login
handler; reading an absent key silently returns `False`, which caused the query filter
`WHERE user_id = <admin_uid>` to always return 0 rows — all 5 existing keys had `user_id IS NULL`
(legacy CLI/admin keys created before M9 §3.3 tenant model).

Beyond the immediate bug, the regression exposed a broader security hole: any authenticated user
could deactivate any API key by ID without an ownership check. An unauthenticated attacker could
not (MCP clients must provide `X-API-Key` header), but a rogue employee could sabotage a colleague's
key.

Additionally, M9 §3.4 specified admin user management (promote/demote routes + UI toggles) and
self-service surfaces for non-admin users, but implementation was incomplete:
`set_user_admin` AuthStore method and `PATCH /api/admin/users/{id}/admin` endpoint were deferred.
The Web UI `/admin/*` surface didn't distinguish between admin and non-admin, causing non-admin
users to see full admin sidebar and making them indistinguishable from admins in the UI.

This ADR closes those gaps via 5 design decisions.

---

## Decision

### 1. Source of Truth for `is_admin` — Database, Never Session

`is_admin` must always be DB-sourced via a new `is_admin_session(request)` helper in
`src/web_ui/auth.py`. This helper wraps the existing `get_user_field()` lookup, fetching the
current row from `webui_users` on every request (cached 5 min via existing `_auth_cache`).

Never read `request.session.get("is_admin")` — the login flow does not write that key (intentional
per ADR-0011, which prescribed DB-sourced admin checks but did not name the helper). Reading an
absent key silently returns `False`, hiding all admin-visible data from legitimate admins.

This rule clarifies ADR-0011 §6 and prevents the regression from recurring.

### 2. `user_id IS NULL` Semantics — System/CLI Key

Existing API keys with `user_id IS NULL` are "system keys" — created before tenant model or by
CLI. These keys are visible to all admins but NOT to any non-admin user. No automatic migration
to claim NULL keys; instead, a new banner appears on `/admin/api-keys`:

> **Unassigned Keys**  
> The following keys were created by administrators and have no owner. Assign an owner to restrict
> access:

Clicking "Assign owner" opens a modal dropdown of non-deactivated `webui_users`, and a PATCH
request sets `user_id`. Once assigned, the key is visible only to that user (self-service) and
to admins (admin-visibility view).

### 3. Surface Split — `/admin/*` (Admins) vs. `/account/*` (Non-Admins)

Add a new `AccountLayout` Astro component (parallel to existing `AdminLayout`) with a slim sidebar
containing only "My API Keys" + "Logout". Non-admin users are redirected from `/admin/*` to
`/account/api-keys` at the middleware level (`src/web_ui/middleware.ts` Astro).

Two pages:
- `GET /account/index` — dashboard (read-only, shows "Profile access: VIEW" status).
- `GET /account/api-keys` — self-service key management (list + create + deactivate own keys).

This provides a clear UX boundary: admins get full `/admin/*` sidebar; non-admins get slim
`/account/*` surface.

### 4. Last-Admin Protection — Refuse Demote/Deactivate if it Leaves 0 Active Admins

Before allowing `set_user_admin(user_id, is_admin=False)` or `set_user_active(user_id, is_active=False)`,
check via SQL `COUNT(*) FROM webui_users WHERE is_admin=TRUE AND is_active=TRUE AND id != $user_id`.
If result is 0, return an error (HTTP 409 Conflict) with a message like:
"Cannot demote or deactivate the last active administrator. Promote another user first."

This prevents the database from ever reaching a state with 0 admins (unrecoverable without direct DB
access or CLI).

### 5. Deferred — `role` Text Column Cleanup

The `webui_users` table contains a stale `role` text column (DEFAULT 'admin', never read by any code).
This cleanup is deferred to M10 or M11 RBAC consolidation (when a broader role-based model may be
introduced). For now, the column is left as-is; callers must use the boolean `is_admin` column.

---

## Consequences

### New Helper
- `is_admin_session(request: Request) -> bool` in `src/web_ui/auth.py` — wraps `get_user_field(username, "is_admin")` with 5-min cache.

### New API Endpoints
- `PATCH /api/admin/users/{id}/admin` — set `is_admin` with last-admin protection and audit log.
- `PATCH /api/admin/api-keys/{id}/owner` — assign `user_id` to a NULL-owner key with audit log.

### New AuthStore Methods
- `set_user_admin(user_id: int, is_admin: bool) -> None` — enforces last-admin check before update.
- `set_user_active(user_id: int, is_active: bool) -> None` — enforces last-admin check before deactivate.

### Web UI Changes
- `AdminLayout` now gates `/admin/*` routes; non-admin users redirect to `/account/api-keys` (via middleware).
- New `AccountLayout` with slim sidebar for non-admin users.
- `/admin/api-keys` shows new "Owner" column; system keys (user_id IS NULL) show "Unassigned" badge + "Assign owner" button.
- `/admin/users` shows new "Admin" toggle (UI for PATCH endpoint); gated to super-admin or self-promotion.
- New `/account/index` dashboard + `/account/api-keys` self-service (non-admin only).

### Deactivate Endpoint Security Fix
- `PATCH /api/api-keys/{id}/deactivate` now enforces ownership check: reject with HTTP 403 if requesting user is not the key's owner AND not an admin.

### Audit Log Extensions (ADR-0021)
- New audit actions: `admin_promote`, `admin_demote`, `key_owner_assign`.

---

## Alternatives Considered

### A — Keep Session `is_admin` Field, Fix Login to Write It

Write `is_admin` to the session during login based on DB read. Simpler for the login flow
(one-time lookup at login time).

**Rejected:** Session fields are only re-read at request time from the signed cookie. If an admin's
`is_admin` flag is toggled by another admin while their browser tab is open, the old session cookie
would remain valid (refreshing the page would re-check). This race is acceptable for most fields
(permissions change takes effect on next login), but admin status is so privileged that the risk
is higher. DB-sourced checks on every request are safer.

### B — Automatic Migration of NULL Keys to Current Admin

When admin logs in, auto-assign all NULL-owner keys to them.

**Rejected:** This violates the principle of least surprise. Admin A creates a system key for
automation; Admin B logs in and suddenly the key appears in their account. When Admin A tries to
use it, it's gone. Interactive assignment (with modal confirmation) is safer.

### C — Audit Log Every Request (not just mutations)

Log read access to `/admin/api-keys` as well.

**Rejected:** Audit logs are for mutations (changes to state). Read-only access should be covered by
application-level request logging or a separate audit trail service, not the mutation log per
ADR-0021.

---

## Related ADRs

- **ADR-0011** — Web UI Session-Based Authentication. This ADR clarifies that `is_admin` source
  of truth is the DB, not the session (Rule 6 prescribed DB-sourced checks; this ADR names the
  helper and prevents regression).
- **ADR-0017** — OAuth. New users default to `is_admin=FALSE` per this ADR's tenant model (M9 §3.3).
- **ADR-0021** — Admin Audit Log. New audit actions added for admin promote/demote/key-assign.
- **ADR-0024** — PATCH Mutation Policy. Consistent with existing PATCH safety rules for profiles
  (preserve `head_sha`, reject mutations on indexed profiles, last-modified safeguards).
