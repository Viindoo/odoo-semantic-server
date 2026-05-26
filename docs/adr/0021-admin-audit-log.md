# ADR-0021 — Admin Audit Log: Canonical Actor Format, Decorator Pattern, Failure Mode

**Status:** Accepted  
**Date:** 2026-05-15  
**Milestone:** M9 W-AL

---

## Context

M9 added multiple security features (W-AC, W-OA, W-MF, W-SG, W-UM, W-BK, W-RS, W-CP, W-FE) that each independently implemented `INSERT INTO admin_audit_log` inline. By integration time there were 9 separate call sites with inconsistent actor formats, action names, and error handling. This ADR consolidates them into a single module.

## Decision

### 1. Module: `src/db/audit.py`

Single source of truth for all audit log writes. Three entry points:

| Entry point | Use case |
|---|---|
| `write_audit_log(actor, action, target, success, detail)` | Direct call (low-level) |
| `@audit_action("action.name")` decorator | FastAPI async route handlers |
| `with audit_cli("action.name") as ctx:` context manager | CLI commands (manager + cli.py) |

### 2. Canonical Actor Format

| Context | Actor string | Example |
|---|---|---|
| Web UI session | `user:<id>` | `user:42` |
| CLI command | `cli:<os_user>` | `cli:alice` |
| MCP API key | `api_key:<prefix>` | `api_key:osm_abc12` |
| OAuth callback | `oauth:<provider>` | `oauth:google` |
| Unauthenticated | `anonymous` | `anonymous` |

### 3. Action Taxonomy

```
user.login             user.login.mfa         user.logout
user.register          user.verify_email      user.reset_password
user.reset_password_link
user.create            user.delete            user.deactivate        user.reactivate
user.set_admin         user.oauth_login

profile.create         profile.update         profile.delete
profile.clone          profile.set_parent     profile.clone_all      profile.assign_tenant

repo.create            repo.update            repo.delete            repo.clone
repo.assign_tenant

tenant.create          tenant.update          tenant.delete
tenant.add_member      tenant.remove_member

api_key.create         api_key.deactivate     api_key.assign_owner

ssh_key.create         ssh_key.import         ssh_key.delete

oauth.login.google     oauth.login.github

totp.setup             totp.verify            totp.disable

operations.backup      operations.restore     operations.apply_preset
operations.index_repo  operations.index_core  operations.seed_patterns
operations.reset_embed operations.index_all

jobs.reset

feedback.submit

fernet.rotate

mcp.query.unscoped
```

`mcp.query.unscoped` — emitted once per MCP tool call that reaches the global/admin
path (`tenant_id IS NULL`).  Per ADR-0034 §D4: "the only unscoped path is
audit-logged."  Fields: `actor="api_key:<prefix>"`, `target=<tool_name>`,
`success=True`, `detail={"tool": <tool_name>}`.  Emitted fire-and-forget by
`UsageLogMiddleware.on_call_tool` in `src/mcp/tool_log_middleware.py`.
Tenant-scoped calls (`tenant_id IS NOT NULL`) are excluded — they are governed
by per-tenant isolation, not the unscoped audit path.

### 4. Failure Mode — Best-Effort, Never Block

`write_audit_log` wraps all DB operations in `try/except Exception` and logs `WARNING` on failure. **The audit log must never raise** — a DB failure during audit INSERT must not break the main request flow (login, restore, etc.).

Rationale: audit log is observability infrastructure, not a security gate. Missing audit rows are less bad than blocking operations.

### 5. Transaction Independence

`write_audit_log` uses `pool.checkout()` to obtain a **dedicated connection** independent of any caller transaction. This ensures a caller ROLLBACK does not lose the audit row. The audit INSERT is committed immediately on its own connection.

### 6. Legacy Columns (W-UM backward-compat)

W-UM introduced `actor_id`, `target_id`, `detail_text` columns alongside the canonical `actor TEXT`, `target TEXT`, `detail JSONB`. The new `write_audit_log` writes only canonical columns. Legacy columns remain in schema for rollback safety. Deprecation plan: remove legacy columns post-M9 in a cleanup PR.

### 7. Decorator Coverage

`@audit_action("action.name")` supports:
- `target_param="param_name"` to extract path param as audit target.
- `request.state.audit_target` — handler-set target for create-style ops whose id is
  generated inside the handler (e.g. `POST /api/admin/users`); takes precedence over
  `target_param`. The handler sets it before returning and must NOT call
  `write_audit_log` directly (that would write a second, duplicate row).
- `request.state.audit_detail` — handler-set dict merged into the audit row's detail
  (safe, non-sensitive fields only). Lets a create/update handler record a forensic
  before/after snapshot through the single decorator-written row.
- Automatic IP + user_agent capture from request headers.
- status_code capture from JSONResponse return value.
- HTTPException → success=False with status_code+reason in detail.
- Unhandled exception → success=False with error_type+error_message in detail.
- `wrapper.__audit_action__` / `__audit_target_param__` introspection markers — used by
  the W3 enumerate-app regression guard to assert every mutating admin route is audited.

### 8. Privacy Constraints

- `feedback.py`: audit action only (`feedback.submit`), **NEVER** include feedback content in audit detail.
- Passwords and tokens are never included in detail.
- User-Agent truncated to 200 chars.
- error_message truncated to 500 chars.

### 9. Retention Policy (TODO)

No automatic cleanup implemented yet. Planned: cron job to DELETE rows older than 90 days. To be added post-M9.

## Consequences

- All new audit writes go through `src/db/audit.py`.
- Existing `AuthStore.log_audit()` is deprecated (docstring updated) but not removed — backward compat for any external callers.
- `_insert_audit_log` local functions in login.py and oauth.py now delegate to `write_audit_log`.
- `_audit_log` in `src/manager/__main__.py` is removed; callers use `audit_cli` context manager.
- Routes in repos.py, api_keys.py, ssh_keys.py, feedback.py gain first-time audit coverage via `@audit_action` decorator.
