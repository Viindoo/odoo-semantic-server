# ADR-0022 — MFA TOTP: Enrollment, Backup Codes, Admin Grace Period

**Status:** Accepted  
**Date:** 2026-05-15  
**Milestone:** M9 W-MF

---

## Context

M9 "Auth Wow" adds multi-user admin and public signup. A single compromised
password should not grant full admin access. TOTP (RFC 6238) is the standard
second factor for web applications; it requires only an authenticator app,
no SMS infrastructure.

---

## Decision

### 1. TOTP via pyotp

Use `pyotp>=2.9` (RFC 6238) with `valid_window=1` (±30 s clock drift tolerance,
i.e., accepts current window plus one window before/after). `pyotp.random_base32()`
generates a 160-bit secret.

### 2. Secret encrypted at rest

The TOTP base32 secret is encrypted with the application's `FERNET_KEY`
(same key used for SSH private keys — see ADR-0004) before storage in
`totp_secrets.secret_encrypted`. Plaintext never written to the database.

### 3. Enrollment flow (two-step, not one-step)

`POST /api/auth/totp/setup` stores the encrypted secret with `enabled=FALSE`.
`POST /api/auth/totp/verify` verifies a code and sets `enabled=TRUE`.
This prevents a user from locking themselves out if they scan the wrong QR code
or enter a typo during setup — TOTP is not active until verified.

### 4. 10 backup codes, HMAC-SHA256 hashed

On first `/verify` success, 10 backup codes are generated via
`secrets.token_hex(8)` (64-bit entropy each).

Each code is hashed with `HMAC-SHA256(WEBUI_SESSION_SECRET, code)` before
storage in `totp_secrets.backup_codes_hash` as a JSONB array:

```json
[{"hash": "<hex>", "used_at": null}, ...]
```

Plaintext codes are returned **once** from `/verify` and never stored.
`hmac.compare_digest` prevents timing attacks on backup code comparison.
`used_at` is set on redemption (single-use enforced).

### 5. Login flow: two-step with signed MFA token

After password verification, if `totp_secrets.enabled = TRUE`:
- Server issues a signed `mfa_token`: `<user_id>:<expires_epoch>.<HMAC-SHA256>`.
- Response: `{mfa_required: true, mfa_token: "..."}` — **no session cookie issued yet**.
- Client calls `POST /api/auth/totp/login` with `{mfa_token, code}` or
  `{mfa_token, backup_code}`.
- On success: full session cookie set (identical to normal login path).

MFA token TTL: 5 minutes. Signed with `WEBUI_SESSION_SECRET` — tamper-proof
without a DB round-trip, but expires quickly to limit exposure window.

Users without TOTP enrolled: login flow unchanged (backward compatible).

### 6. Admin grace period (7 days)

`AuthRequiredMiddleware` checks for admin users (`webui_users.is_admin = TRUE`)
with no enabled TOTP and account age > `MFA_GRACE_DAYS = 7` days.

If enforcement triggers: returns HTTP 403 `{error: "mfa_required",
redirect: "/admin/security?force_mfa=1"}`. Astro front-end middleware handles
the redirect. MFA setup/verify API paths are exempt from this check to avoid
a redirect loop.

Tests bypass enforcement via `WEBUI_AUTH_DISABLED=1 + PYTEST_CURRENT_TEST`
(same dual-env guard as existing auth bypass).

---

## Consequences

- `pyotp` + `qrcode[pil]` added to `pyproject.toml` dependencies.
- `migrations/m9_007_totp_secrets.sql`: new table, applied by yoyo.
- `/api/auth/totp/*` routes exempt from auth middleware (prefix `/api/auth/`).
- `/api/auth/totp/login` is public (pending session state).
- `_check_mfa_enforcement()` performs a DB query per authenticated request for
  admin users. This is a constant-time check (1 row lookup with indexed FK);
  acceptable for admin-only traffic volume. Can be cached per session if needed.
- `FERNET_KEY` required at startup — already enforced by SSH key routes.
- Backward compat: users with no `totp_secrets` row → MFA step skipped entirely.
