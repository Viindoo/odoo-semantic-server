# Milestone 9 — "Auth Wow"

**Status:** Planning (chưa start)
**Created:** 2026-05-12
**Prerequisite:** M8 "Astro Unified" (`2026-05-12-milestone-8-astro-unified.md`) fully merged.

---

## 1. Intent

Public signup, OAuth login, multi-user admin, and self-serve account operations. Zero Jinja2 migration debt — M8 already removed Jinja2 and migrated all admin UI to Astro SSR. M9 is pure feature development on top of the unified Astro + FastAPI JSON API stack.

---

## 2. Stack Baseline (inherited from M8 + post-PR #98)

- **Frontend:** Astro 6.x + `@astrojs/node` 10.x in `site/`, `output: 'server'` (`'hybrid'` merged into `'server'` in Astro 5.x and unchanged in 6.x). Admin pages at `/admin/*` (SSR), landing pages static via per-page `prerender = true`.
- **Backend:** FastAPI pure JSON API at `/api/*` on port 8003. SessionMiddleware + bcrypt auth.
- **Auth:** Cookie-based session (signed, SameSite=strict, HttpOnly, 8h TTL). Session verification via `GET /api/auth/verify` from Astro middleware.
- **OAuth libraries:** `arctic` + `oslo` (Node.js, native Astro SSR support — no Python OAuth needed in M8 base). When installing, verify peer-dep compat with Astro 6.
- **Toolchain (post-PR #98):** Node.js ≥ 22.12.0 (Astro 6 requirement), pnpm ≥ 10 (workspace `overrides:` + `allowBuilds:` fields). CI pins Node 22 + pnpm 10. `devalue` forced to ≥ 5.8.1 via `pnpm-workspace.yaml` `overrides` (CVE-2026-42570). Do not regress these versions when adding deps.

---

## 3. Items

### 3.1 OAuth Integration (Google + GitHub)

**Astro side:**
- Install `arctic` + `oslo` in `site/package.json`
- `site/src/pages/admin/auth/google.ts` — redirect to Google OAuth consent
- `site/src/pages/admin/auth/callback/google.ts` — receive code → exchange → create session
- Same for GitHub (`/admin/auth/github`, `/admin/auth/callback/github`)
- Update `site/src/pages/admin/login.astro` — add "Sign in with Google" + "Sign in with GitHub" buttons

**FastAPI side:**
- `POST /api/auth/oauth-login` — receive OAuth provider + access_token from Astro → verify with provider → upsert `webui_users` row → return session cookie
- `webui_users` table: add `oauth_provider VARCHAR(32)`, `oauth_id VARCHAR(255)` columns (nullable; NULL = password-based)
- Migration: `yoyo` migration for new columns

**Config:**
- `.env` / `webui.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
- Callback URLs: `https://<host>/admin/auth/callback/google` (prod) / `http://localhost:4321/admin/auth/callback/google` (dev)

**ADR:** `docs/adr/0014-oauth-arctic-oslo.md` — OAuth via arctic+oslo in Astro SSR; FastAPI only does final token verification + session issuance; no Python OAuth library.

### 3.2 Public Signup + Email Verification

**Astro:**
- `site/src/pages/signup.astro` — public page (`export const prerender = false`), no auth guard
- `site/src/pages/verify-email.astro` — receives `?token=...` from email link

**FastAPI:**
- `POST /api/auth/register` — create unverified user, send verification email (SMTP)
- `POST /api/auth/verify-email` — receive token → mark user verified → issue session
- `webui_users` table: add `email_verified BOOLEAN DEFAULT FALSE`, `verification_token VARCHAR(64)`, `created_at TIMESTAMP`
- SMTP config: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` in `.env`

Email template: minimal HTML (no Jinja2 — Python string template or inline f-string OK for single email).

### 3.3 Tenant API Key Issuance

After signup + email verification, users can self-serve create API keys from dashboard:
- `site/src/pages/admin/api-keys.astro` update — show "Create key" button (already exists for admin)
- `GET /api/api-keys` — filter by `user_id` (add `user_id FK` to `api_keys` table)
- `POST /api/api-keys` — associate new key with current user from session
- Admin-created keys (CLI `create-api-key`) get `user_id = NULL` (global/admin keys, backward compat)

Migration: `api_keys` table add `user_id INT FK webui_users(id) ON DELETE CASCADE NULLABLE`.

### 3.4 Self-Serve User Management

Admin-only section in Web UI (only visible to users with `is_admin = TRUE`):
- `site/src/pages/admin/users.astro` — list users, deactivate, reset password link
- `GET /api/admin/users` — list all `webui_users`
- `POST /api/admin/users/{id}/deactivate` — set `active = FALSE`
- `POST /api/admin/users/{id}/reset-password-link` — generate reset token + send email

`webui_users` table: add `is_admin BOOLEAN DEFAULT FALSE`, `active BOOLEAN DEFAULT TRUE`.
`create-webui-user` CLI: add `--admin` flag to set `is_admin = TRUE`.

### 3.5 Backup/Restore Web UI

Existing CLI: `python -m src.cli backup` + `restore`. M9 adds Web UI trigger:
- `site/src/pages/admin/operations.astro` update — add "Backup" + "Restore" sections (React component for drag-drop upload)
- `POST /api/operations/backup` — trigger `src.cli.backup()` as subprocess, stream logs via SSE or poll job status
- `POST /api/operations/restore` — receive `.tar.gz` upload, trigger `src.cli.restore()` — SECURITY REVIEW required (file size limit, extension check, path traversal guard)
- Restore requires `is_admin = TRUE` (from §3.4)

**Security gates before shipping restore upload:**
- Max file size: 500MB hard limit in FastAPI multipart
- Extension allow-list: `.tar.gz` only
- No `..` in extracted paths (`tarfile.extractall` with member path check)
- Audit log entry for every restore attempt

### 3.6 FERNET Key Rotation Web UI

**Deferred to M9 late or M10** — requires 2FA + audit log first (high-risk operation). Placeholder UI only in M9:
- `operations.astro` — show "FERNET Rotation" section as "Requires 2FA — coming soon" if 2FA not set up.

Actual rotation: `POST /api/admin/rotate-fernet` — wraps `src.cli.rotate_fernet()`. Only callable with `is_admin = TRUE` + valid 2FA token.

### 3.7 CLI delete-profile / delete-repo

CLI parity for operations already available in Web UI (after M8 Stream X):
- `python -m src.manager delete-profile <id>` — calls existing DB delete logic
- `python -m src.manager delete-repo <id>` — calls existing DB delete logic
- Files: `src/manager/__main__.py` — add two new subcommands

No Astro/FastAPI changes needed. Pure Python CLI.

### 3.8 DB Migration Trigger UI

- `operations.astro` — add "Migrations" section: display current yoyo migration version (from DB), list pending migrations
- `GET /api/admin/migrations` — query yoyo `_yoyo_migration` table for applied migrations
- No trigger button in M9 (trigger stays in deploy script per ADR-0001 policy). Display + info only.

---

## 4. Worktree Topology

```
master ┬── W1 (OAuth) ── W2 (signup + email)   ← linear (signup depends on OAuth user model)
       ├── W3 (tenant API keys)                  ← independent (depends only on M8 api_keys route)
       ├── W4 (user management)                  ← independent (new DB columns + routes)
       ├── W5 (backup/restore UI)                ← independent (wraps existing CLI)
       └── W6 (CLI delete + DB migration UI)     ← independent (pure additions)
```

W1 + W3 + W4 + W5 + W6 can be dispatched in parallel (first message). W2 dispatched after W1 lands (synthetic base if needed).

**Agent assignments:**
- W1 (OAuth): Sonnet (arctic + oslo integration, token exchange, new DB columns)
- W2 (signup + email): Sonnet (email template, verification flow)
- W3 (tenant API keys): Haiku (mechanical: DB column + filter + route update)
- W4 (user management): Sonnet (is_admin gating, new admin routes)
- W5 (backup/restore UI): Sonnet (security review, file upload, SSE streaming)
- W6 (CLI + DB migration UI): Haiku (mechanical: subcommands + read-only display)

---

## 5. New DB Migrations (yoyo)

| Migration | Changes |
|-----------|---------|
| `m9_001_oauth_columns.sql` | `webui_users`: add `oauth_provider`, `oauth_id`, `email_verified`, `verification_token`, `created_at`, `is_admin`, `active` |
| `m9_002_api_keys_user_fk.sql` | `api_keys`: add `user_id INT FK webui_users(id) ON DELETE CASCADE NULLABLE` |

All migrations: backwards-compatible (nullable columns, boolean with default).

---

## 6. Config Additions

```bash
# .env / webui.env — new in M9
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=noreply@odoo-semantic.viindoo.com
```

`.env.example`: Add all with `<placeholder>` values. `webui.env` (secrets) gets OAuth + SMTP credentials.

---

## 7. ADRs

- **ADR-0014:** `docs/adr/0014-oauth-arctic-oslo.md` — OAuth provider integration via arctic+oslo (Astro SSR); FastAPI issues session after token exchange; no Python OAuth library needed.

---

## 8. Acceptance Criteria

- [ ] "Sign in with Google" on `/admin/login` → OAuth flow → authenticated session
- [ ] "Sign in with GitHub" same
- [ ] `POST /api/auth/register` → email sent with verification link
- [ ] Click verification link → user verified → auto-login
- [ ] Verified user can create own API key from `/admin/api-keys`
- [ ] Admin (is_admin=TRUE) sees `/admin/users` page
- [ ] Admin can deactivate a user → deactivated user login fails
- [ ] `python -m src.manager delete-profile <id>` removes profile
- [ ] `python -m src.manager delete-repo <id>` removes repo
- [ ] `/admin/operations` shows current DB migration version (read-only)
- [ ] Backup download button → file downloaded
- [ ] Restore upload: valid `.tar.gz` accepted; `../` path → 400 rejected
- [ ] `make lint && make test` green
- [ ] `pnpm run check` green in `site/`
- [ ] ADR-0014 committed
