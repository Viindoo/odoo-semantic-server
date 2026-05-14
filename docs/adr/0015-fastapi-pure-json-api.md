# ADR-0015: FastAPI Pure JSON API

**Status:** Accepted
**Date:** 2026-05-14
**Deciders:** M8 engineering

---

## Context

M7.5 shipped a Jinja2 server-side rendered (SSR) Web UI at port 8003. The UI rendered HTML templates for all admin pages: dashboard, repos, API keys, SSH keys, and operations.

M8 "Public Wow" introduces an Astro SSR frontend (ADR-0014) to serve the admin interface as a modern, polished single-page application. Astro handles all HTML rendering; the FastAPI backend is responsible only for data, not presentation.

Keeping Jinja2 alongside Astro would create two sources of truth for the admin UI and add unnecessary complexity. Jinja2 is ~350KB of dependency that no longer serves a purpose once the Astro layer owns HTML.

---

## Decision

1. **Remove Jinja2 entirely** from `src/web_ui/`. Delete all 7 `.html` templates under `src/web_ui/templates/`. Remove `jinja2>=3.1,<4.0` from `pyproject.toml`.

2. **All routes return JSON.** Every endpoint in `src/web_ui/routes/` now returns `JSONResponse` (or raises `HTTPException`). No `TemplateResponse` or `HTMLResponse` anywhere.

3. **URL prefix convention: `/api/*`.**
   - Auth: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/verify`
   - Dashboard: `GET /api/dashboard/stats`
   - Repos: `GET/POST /api/repos/profiles`, `DELETE /api/repos/profiles/{id}`, `GET/POST /api/repos/repos`, `DELETE /api/repos/repos/{id}`, `GET /api/repos/repos/{id}/clone-status`, `POST /api/repos/repos/{id}/index`, `POST /api/repos/repos/{id}/reset-embed`, `POST /api/repos/index-all`, `GET /api/repos/jobs/{id}/status`, `POST /api/repos/jobs/{id}/reset`
   - API Keys: `GET/POST /api/api-keys`, `POST /api/api-keys/{id}/deactivate`
   - SSH Keys: `GET/POST /api/ssh-keys`, `POST /api/ssh-keys/import`, `DELETE /api/ssh-keys/{id}`
   - Operations: `GET /api/operations/presets`, `POST /api/operations/index-core`, `POST /api/operations/seed-patterns`, `POST /api/operations/apply-preset`
   - Feedback: `POST/GET /api/feedback`

4. **`AuthRequiredMiddleware` returns 401 JSON** instead of `302 Redirect` to `/login`. Exempt prefix changes from `/login`, `/logout`, `/static/` to `/api/auth/`.

5. **`GET /api/auth/verify`** — new endpoint consumed by Astro middleware to check if the session cookie is valid before serving protected pages. Returns `{"ok": true, "username": "..."}` (200) or `{"ok": false, "error": "not_authenticated"}` (401). This is the Astro-to-FastAPI auth proxy pattern.

6. **Session cookie mechanism unchanged.** `starlette.middleware.sessions.SessionMiddleware` with `osm_session` cookie, bcrypt cost=12, 8h TTL, `SameSite=strict`. Loopback-only middleware preserved (Astro backend also runs loopback). `WEBUI_SESSION_SECRET`, `WEBUI_SECURE_COOKIE=0` dev flag — all unchanged.
   - **Post-merge correction (2026-05-14):** the original M8 risk model only considered Astro→FastAPI as the loopback path. It missed the **browser → nginx /api/* → FastAPI** path. uvicorn's `ProxyHeadersMiddleware` (default `proxy_headers=True`, trusted hosts `127.0.0.1`) rewrites `scope["client"]` to the value of `X-Forwarded-For` forwarded by nginx — so `request.client.host` becomes the real external IP and `_LoopbackOnlyMiddleware` returns 403 for every external `/api/*` request. Fix: invoke uvicorn with `proxy_headers=False` (see `src/web_ui/__main__.py`); read `X-Real-IP` explicitly in places that genuinely need the real client IP (login rate limit). `SameSite=strict` on the session cookie remains the CSRF mitigation; LoopbackOnly stays as a defense-in-depth guard against direct external TCP connections to port 8003.

7. **`python-multipart` kept** in deps — still needed for any future form parsing, but all routes now accept JSON bodies via Pydantic models.

---

## Consequences

**Positive:**
- Jinja2 removed from dependency tree (~350KB, one fewer template engine).
- All business logic testable without a browser (pure JSON assertions).
- Astro frontend can be developed and deployed independently.
- `GET /api/auth/verify` enables clean Astro middleware session check without sharing session secret.
- Tests simplified: no HTML parsing, no `"text" in resp.text` assertions.

**Negative / Tradeoffs:**
- Any client that was calling the old Jinja2 HTML routes (e.g., custom scripts hitting `/repos`) must migrate to `/api/repos/profiles` etc.
- `POST /login` (form submit) replaced by `POST /api/auth/login` (JSON body). Browser form-based login no longer supported directly; Astro login page calls the JSON API.
- `303 Redirect + flash` pattern replaced by `200/4xx JSON` + `error` field. Astro handles redirect and flash display.

**Test changes:**
- All `test_web_ui_*.py` (non-browser) updated to call `/api/*` endpoints with `json=` bodies and assert on JSON response fields.
- Status code assertions changed: `303 → 200 ok` for success, `303 flash → 422/409 error JSON` for validation/conflict, `404 redirect → 404 JSON` for not found.
- Browser tests (`*_browser.py`) unchanged — they test the Astro layer.

---

## References

- ADR-0011: Web UI session auth (bcrypt cost=12, 8h TTL, cookie policy)
- ADR-0014: Astro unified frontend (M8 public wow)
- ADR-0004: Auth web UI SSH policy
