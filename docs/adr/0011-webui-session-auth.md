# ADR-0011 — Web UI Session-Based Authentication

**Date:** 2026-05-11  
**Status:** Accepted

## Context

The Web UI admin interface (port 8003) previously relied entirely on SSH tunnel / IP
allowlist for access control. The loopback-only middleware (ADR-0004 §I6) ensures the
port is not reachable from external addresses when bound to `127.0.0.1`, but provides
no defence if:

- An attacker gains local process access on the server (privilege escalation).
- The operator exposes port 8003 behind an SSH tunnel shared with multiple users.
- A misconfigured reverse proxy accidentally forwards requests to port 8003.

The Web UI allows creating/deleting API keys, SSH keypairs, and triggering repo
indexing — all high-impact administrative actions. A second authentication factor
(login + password) provides defence-in-depth without requiring MFA or PKI.

## Decision

### 1. Password hashing — bcrypt cost=12

bcrypt is chosen over Argon2id for simplicity of deployment (pure-Python `bcrypt`
package, no native library required at runtime). Cost=12 yields ~250–400 ms on a 2025
server CPU, which is acceptable for a single admin login. It is slow enough to resist
offline dictionary attacks on a stolen DB row.

Argon2id is marginally better for large-scale user stores; for a single-admin tool with
<10 concurrent logins per day, bcrypt cost=12 is sufficient. Revisit if user count
exceeds 50.

### 2. Session cookie — Starlette SessionMiddleware + itsdangerous

Starlette's `SessionMiddleware` stores a signed, tamper-evident cookie using
`itsdangerous.URLSafeTimedSerializer`. The session payload is stored client-side
(signed but NOT encrypted); it contains only `{username, session_at}`. No sensitive
data lives in the cookie.

Cookie flags:
- `SameSite=strict` — prevents CSRF via cross-site navigation.
- `HttpOnly=True` (Starlette default) — prevents JavaScript access.
- `Secure=True` — cookie only sent over HTTPS. In local dev over plain HTTP the cookie
  is technically blocked by `Secure`; the test suite bypasses this via `httpx.AsyncClient`
  ASGI transport (no real TLS handshake). For production over SSH tunnel or Nginx TLS
  this flag is correct.

### 3. Session TTL — 8 hours

Enforced server-side: `session_at` epoch stored in session dict; each request checks
`time.time() - session_at < SESSION_TTL_SECONDS (28800)`. Cookie max_age is `None`
(session cookie — expires on browser close). This ensures a combination of:
- Browser tab close → cookie gone.
- Long-lived browser process → session expires after 8 h of idle.

### 4. No JWT

JWT would require generating + verifying tokens, handling key rotation, and managing
revocation lists. For a localhost-only admin UI with a single persistent session, the
overhead is not justified. Starlette sessions are sufficient.

### 5. No MFA

MFA (TOTP) deferred to M8 / future. Current threat model: the admin user is a trusted
developer with SSH access to the server; MFA would require additional UX complexity
(QR code setup, backup codes) disproportionate to the gain.

### 6. User storage — `webui_users` PostgreSQL table

`webui_users (username VARCHAR(64) PRIMARY KEY, password_hash VARCHAR(255))` added to
the same PostgreSQL instance as other registry tables. A dedicated column avoids
conflating web UI users with MCP API keys. The schema addition is managed via
`migrations/9000_webui_users.sql` (W15 yoyo migration system compatibility).

### 7. Bootstrap — `create-webui-user` CLI

An admin who needs to set up the first user would be locked out if login is required
before a user exists. `python -m src.manager create-webui-user admin` runs outside the
Web UI and inserts the first row. It prompts interactively via `getpass` (password never
in process arguments or shell history).

Existing-deploy recovery: if an admin is locked out, run
`python -m src.manager create-webui-user admin --reset` on the server directly.

## Consequences

- Web UI access now requires `create-webui-user` as a first-time setup step.
- bcrypt and itsdangerous are new runtime dependencies (added to `pyproject.toml`).
- Tests use `httpx.AsyncClient(app=...)` ASGI transport; Secure cookie flag does not
  block ASGI-level tests (no real TLS).
- `WEBUI_SESSION_SECRET` must be set in `webui.env` for production. If unset, a
  per-process random secret is generated with a loud warning (sessions invalidated on
  restart).
- All routes except `/login`, `/logout`, `/static/*`, `/health` now require auth.
