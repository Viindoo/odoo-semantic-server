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

## Amendment (feat/m10b-auth-unify, 2026-05-29)

- `/login` is the **canonical** login URL. `/admin/login` is retained only as a GET-only 301 redirect
  to `/login` (backward-compat shim).
- The Astro middleware auth-gate bounce target for unauthenticated requests is `/login` (was
  `/admin/login`); `/account/*` and nginx return-redirects target `/login` likewise.
- OAuth init + callback paths `/admin/auth/*` are **unchanged** (no provider-console reconfig).

## Amendment — SameSite Strict → Lax (fix/auth-ux-oauth-cache-plans, 2026-05-29)

**Decision change:** `osm_session` cookie changed from `SameSite=Strict` to `SameSite=Lax`.
This supersedes the `SameSite=strict` decision in §2 above.

### Root cause

The OAuth redirect chain (e.g. `accounts.google.com → /admin/auth/callback → /admin/`) is a
cross-site top-level navigation.  Browsers that implement the SameSite spec correctly withhold
`SameSite=Strict` cookies on every hop that crosses origins — including the final redirect back
to this application.  As a result the session cookie written at the end of the OAuth handshake
was never sent on the first authenticated page load, and the user appeared logged out despite a
successful OAuth exchange.

### Security trade-off

`SameSite=Lax` still blocks the primary CSRF threat: cross-site **POST**, PUT, DELETE, and
subresource requests (images, XHR, fetch) never carry the cookie.  Only top-level GET
navigations (link clicks, address-bar loads, OAuth redirects) are allowed — which is exactly
the case needed here.

This application has two additional mitigating controls that make Lax safe:

1. **Loopback-only FastAPI** — `_LoopbackOnlyMiddleware` rejects any request that does not
   arrive on `127.0.0.1`; the session cookie can only be used via nginx reverse-proxy or SSH
   tunnel, not by a third-party origin directly.
2. **Astro same-origin proxy** — all HTML is served by Astro (port 4321) behind the same
   nginx origin; there is no cross-origin surface that could exploit a Lax cookie for
   state-changing reads.

No new vulnerabilities are introduced by this change; `Strict` was providing defence-in-depth
against an attack vector that is already closed by the loopback middleware.
