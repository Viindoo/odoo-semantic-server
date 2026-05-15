# ADR-0017 — OAuth Google + GitHub via arctic + oslo (Node-native)

**Status:** Accepted  
**Date:** 2026-05-15  
**Milestone:** M9 W-OA

---

## Context

M9 introduces OAuth login (Google, GitHub) so new users can sign up without
creating a password-based account. The OAuth flow lives in Astro SSR (Node.js
runtime) while the session store lives in FastAPI (Python). Two components need
to be coordinated:

1. **Authorization URL generation + callback handling** — Astro server endpoints
   that redirect the browser to the provider and handle the callback.
2. **User upsert + session issuance** — FastAPI endpoint that receives validated
   user info from Astro and writes to Postgres.

---

## Alternatives Considered

### A — Python-only flow (authlib / oauthlib in FastAPI)

FastAPI handles every OAuth step including the redirect. Astro would proxy or
redirect to `/api/auth/oauth/google` on FastAPI.

**Rejected:** Adds a cross-origin cookie relay problem. The session cookie must
be set on the Astro origin (port 4321) for the browser's SameSite=strict policy
to work. A Python-only flow would require CORS configuration and two hop cookies.

### B — Astro middleware + custom PKCE (no external library)

Manual implementation of PKCE code_verifier, code_challenge, state generation.

**Rejected:** Re-implementing PKCE + state generation correctly is error-prone.
Using an audited library is lower risk.

### C — arctic (Node) for Astro + FastAPI handles session only (chosen)

`arctic` (OSS, MIT) is a TypeScript OAuth 2.0 client library purpose-built for
server-side frameworks (Astro, Next.js, SvelteKit). It handles:
- PKCE code_verifier + code_challenge generation (`generateCodeVerifier`)
- State generation (`generateState`)
- Authorization URL construction
- Token exchange (`validateAuthorizationCode`)

FastAPI receives only the validated user info (via loopback POST) and handles
session issuance — the part it already owns.

---

## Decision

Use **arctic 3.x** for OAuth flow in Astro SSR endpoints.

`oslo` (1.x, deprecated) was added as a peer dependency but its cookie helpers
are not needed — Astro's built-in `cookies.set()` API covers state and verifier
cookie management. oslo will be removed in M9.1 when the oslojs.dev successor
packages are stable.

---

## Security Gates (F5)

F5 finding from the M9 security audit mandates state + PKCE on every OAuth
callback. Implementation:

| Gate | Where enforced |
|------|----------------|
| `state` CSRF validation | Astro callback: `state !== storedState → 403` |
| PKCE `code_verifier` | Astro callback: passed to `google.validateAuthorizationCode(code, verifier)` |
| State + verifier cookies | `HttpOnly=true`, `Secure=true`, `SameSite=lax`, `maxAge=600` |
| Single-use cookies | Deleted immediately at callback start before token exchange |
| Loopback-only API | FastAPI `_LoopbackOnlyMiddleware` blocks non-loopback callers of `/api/auth/oauth-login` |

GitHub does not support PKCE (as of arctic 3.x) — only state validation applies.

---

## Account Linking Matrix

| Existing account | Provider `email_verified` | Action |
|-----------------|--------------------------|--------|
| None | any | Create new OAuth-only user (`password_hash = NULL`, `is_admin = FALSE`) |
| Match by `(oauth_provider, oauth_id)` | any | Fast path: issue session |
| Match by email | TRUE | Merge: set `oauth_provider` + `oauth_id` on existing account |
| Match by email | FALSE | Reject 409: prevents account takeover via unverified email |

**Rationale for the email+unverified reject:** An attacker controlling a GitHub
account with unverified email `victim@example.com` could claim ownership of an
existing admin account. Requiring `email_verified=true` from the provider closes
this vector. The 409 response includes a human-readable message directing the
user to verify their email at the provider first.

---

## Session Issuance

The Astro callback calls `POST /api/auth/oauth-login` (loopback). FastAPI:
1. Resolves / creates the user record.
2. Calls `_create_session()` → inserts into `active_sessions`.
3. Sets `request.session["session_id"]` / `username` / `session_at` in the
   signed Starlette `SessionMiddleware` cookie.
4. Returns 200 — the Astro callback forwards the `Set-Cookie` header to the
   browser and redirects to `/admin/`.

OAuth-only users (password_hash IS NULL) cannot use the password login path —
`login.py` must guard this: if `pw_hash is None`, reject with
`invalid_credentials` (same error as wrong password — no information leak).

---

## Audit Log

Every oauth-login attempt (success, failure, 409 conflict) writes to
`admin_audit_log` with:
- `actor`: `oauth:<provider>:<oauth_id>` (pre-login) or `user:<id>` (post-login)
- `action`: `user.oauth_login`
- `detail`: `{ip, provider, reason?}`

---

## Consequences

- `arctic 3.7.0` added to `site/package.json` (Node ESM, no polyfill needed for
  Astro SSR on Node 22+).
- `oslo 1.2.1` added as transitional dependency; marked deprecated upstream.
  Will be replaced with `@oslojs/crypto` + `@oslojs/encoding` in a future PR
  when those packages stabilise.
- Four new Astro server endpoints: `admin/auth/{google,github}.ts` +
  `admin/auth/callback/{google,github}.ts`.
- One new FastAPI route: `src/web_ui/routes/oauth.py` registered in `app.py`.
- `.env.example` extended with `GOOGLE_*`, `GITHUB_*`, `PUBLIC_BASE_URL`,
  `API_BASE_URL`.
