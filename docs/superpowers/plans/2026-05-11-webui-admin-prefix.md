# Web UI under `/admin/` path prefix ⚠️ SUPERSEDED

> **Status:** ⊘ SUPERSEDED — folded into M8 astro-unified

> **SUPERSEDED 2026-05-12** — FastAPI `root_path` refactor approach dropped. Admin prefix is now handled by Astro routing (Astro serves `/admin/*` via SSR). FastAPI becomes pure JSON API at `/api/*`.
> See authoritative plan: [`2026-05-12-milestone-8-astro-unified.md`](2026-05-12-milestone-8-astro-unified.md) Stream A (FastAPI JSON API) + Stream B (Astro admin pages).
>
> This file is kept for historical reference only. Do NOT implement from this file.

---

# Web UI under `/admin/` path prefix (original 2026-05-11 plan — HISTORICAL)

**Status:** plan-only, not yet implemented — **SUPERSEDED**
**Created:** 2026-05-11
**Author/operator:** Viindoo team (orchestrated by Claude Code session)
**Related ADRs:** ADR-0011 (Web UI session auth, M7 W16); new ADR-0012 to be written as part of this work.

---

## 1. Goal

End-state URLs on the production host `odoo-semantic.viindoo.com`:

| URL | Owner | Status after this work |
|-----|-------|------------------------|
| `https://odoo-semantic.viindoo.com/mcp` | MCP HTTP endpoint | unchanged |
| `https://odoo-semantic.viindoo.com/health` | MCP health probe | unchanged |
| `https://odoo-semantic.viindoo.com/install/` | MCP install onboarding page | unchanged |
| `https://odoo-semantic.viindoo.com/api/feedback{,/...}` | MCP feedback API | unchanged |
| **`https://odoo-semantic.viindoo.com/admin/`** | **Web UI (new exposure)** | **proxied via nginx to 127.0.0.1:8003** |
| `https://odoo-semantic.viindoo.com/` | reserved for future public landing site | nginx returns 404 today; documented as future scenario-A static landing |

Dev workflow after this change:
- Web UI accessed at `http://127.0.0.1:8003/admin/` (NOT `/`).
- All tests, runbooks, README snippets updated to reflect this.

## 2. Why this approach

User-locked decision (2026-05-11):
- **Hard-code `/admin`** (no `WEBUI_ROOT_PATH` env). Simpler, fewer code paths, single canonical URL. Cost: dev URL also has the prefix.
- **Session cookie**: try `path=/admin`, fallback `path=/` if Starlette's pinned `SessionMiddleware` doesn't accept the `path=` kwarg.
- **Refactor pattern**: FastAPI `root_path="/admin"` + named routes + `request.url_for()` for redirects + Jinja `url_for` for templates. This is the canonical FastAPI-behind-proxy pattern and survives any future path change with a single constant.

Alternative patterns rejected:
- `app.mount("/admin", webui)` — doesn't fix outgoing redirects/links.
- Custom `url(path)` helper — duplicates Starlette's `url_for`, more maintenance.
- nginx `sub_filter` rewriting — fragile under gzip/compression, breaks on HTML changes.

## 3. Future website at `/` (deferred, not in this plan)

Per Sonnet survey (read-only) 2026-05-11: recommended approach is **scenario A — static landing served by nginx**. One-line change to current vhost: replace `location / { return 404; }` with `location / { root /var/www/odoo-semantic-landing; try_files $uri $uri/ /index.html; }`. nginx longest-prefix matching guarantees no conflict with `/mcp`, `/install`, `/admin/`, `/health`, `/api`.

This work does NOT implement the landing site. The current `location / { return 404; }` STAYS as-is. We just guarantee `/admin/` plan doesn't block future scenario A.

---

## 4. Code changes (exhaustive, hard-coded `/admin`)

Path constant: define once in `src/web_ui/app.py`:

```python
ADMIN_PREFIX = "/admin"
```

Used by `FastAPI(root_path=ADMIN_PREFIX)`, `uvicorn.run(root_path=ADMIN_PREFIX)`, `SessionMiddleware(path=ADMIN_PREFIX)`, and the `safe_next` validator. NOT exposed as env var (per user decision).

### 4.1 App wiring

- `src/web_ui/app.py:30-35` — set `FastAPI(..., root_path=ADMIN_PREFIX)`.
- `src/web_ui/__main__.py:24-27` — `uvicorn.run(app, ..., root_path=ADMIN_PREFIX)`.
- `src/web_ui/app.py:59-66` — `SessionMiddleware(..., path=ADMIN_PREFIX)`. Try first; if Starlette version pinned doesn't accept the kwarg, fall back to default (`path="/"`) and add a TODO comment + capture in ADR-0012.

### 4.2 Named routes (17 decorators)

Add `name="..."` to every route decorator so `url_for(name)` works:

| File | Line | Route | Name |
|------|------|-------|------|
| `src/web_ui/routes/dashboard.py` | 49 | `/` | `dashboard` |
| `src/web_ui/routes/login.py` | 77 | `/login` GET | `login_get` |
| `src/web_ui/routes/login.py` | 96 | `/login` POST | `login_post` |
| `src/web_ui/routes/login.py` | 148 | `/logout` | `logout` |
| `src/web_ui/routes/repos.py` | 31 | `/repos` | `repos_page` |
| `src/web_ui/routes/repos.py` | 61 | `/repos/profiles` POST | `create_profile` |
| `src/web_ui/routes/repos.py` | 81 | `/repos/repos` POST | `add_repo` |
| `src/web_ui/routes/repos.py` | 175 | `/repos/ssh-keys-list` | `ssh_keys_list_json` |
| `src/web_ui/routes/repos.py` | 190 | `/repos/repos/{id}/clone-status` | `clone_status_json` |
| `src/web_ui/routes/repos.py` | 215 | `/repos/repos/{id}/index` POST | `index_repo` |
| `src/web_ui/routes/repos.py` | 253 | `/repos/jobs/{id}/status` | `job_status_json` |
| `src/web_ui/routes/api_keys.py` | 30 | `/api-keys` | `api_keys_page` |
| `src/web_ui/routes/api_keys.py` | 56 | `/api-keys` POST | `create_api_key` |
| `src/web_ui/routes/api_keys.py` | 90 | `/api-keys/{id}/deactivate` POST | `deactivate_api_key` |
| `src/web_ui/routes/ssh_keys.py` | 69 | `/ssh-keys` | `ssh_keys_page` |
| `src/web_ui/routes/ssh_keys.py` | 102 | `/ssh-keys` POST | `create_ssh_key` |
| `src/web_ui/routes/ssh_keys.py` | 148 | `/ssh-keys/{id}/delete` POST | `delete_ssh_key` |

Confirm line numbers immediately before editing — repo may have drifted.

### 4.3 Python redirects (~12 sites)

Replace literal-string `RedirectResponse` with `request.url_for()`:

- `src/web_ui/routes/login.py:84` → `RedirectResponse(request.url_for("dashboard"), 302)`
- `src/web_ui/routes/login.py:116-117` `safe_next` default — must come from `request.scope["root_path"] or "/"`. The validator that accepts `?next=` must require `next.startswith(ADMIN_PREFIX + "/")` AND reject `//` (open-redirect guard).
- `src/web_ui/routes/login.py:123,137` `f"/login?error=..."` — build with `url_for("login_get")` + query string.
- `src/web_ui/routes/login.py:145` `RedirectResponse(safe_next, 302)` — `safe_next` already absolute.
- `src/web_ui/routes/login.py:152` `"/login"` → `url_for("login_get")`.
- `src/web_ui/middleware.py:79-82` — login redirect: use `request.url_for("login_get")` + `?next=...`.
- `src/web_ui/routes/repos.py:78,148,172,250` — `url_for("repos_page")`. Line 147 has `?flash=...` query — concat.
- `src/web_ui/routes/repos.py:236` `f"/repos?flash=..."` — same.
- `src/web_ui/routes/api_keys.py:106` → `url_for("api_keys_page")`.
- `src/web_ui/routes/ssh_keys.py:161` → `url_for("ssh_keys_page")`.

### 4.4 Templates

Use Jinja `{{ url_for('name', **kwargs) }}` for hrefs and form actions:

- `src/web_ui/templates/base.html:42-50` — 5 nav links + active-link check. Replace `request.url.path == "/"` with `request.scope["route"].name == "dashboard"` (more robust, doesn't break under prefix changes).
- `src/web_ui/templates/login.html:95` → `action="{{ url_for('login_post') }}"`.
- `src/web_ui/templates/api_keys.html:23,55` → `url_for('create_api_key')`, `url_for('deactivate_api_key', key_id=k.id)`.
- `src/web_ui/templates/repos.html:18,42,74,119` → `url_for('create_profile')`, `url_for('add_repo')`, `url_for('ssh_keys_page')`, `url_for('index_repo', repo_id=r.id)`.
- `src/web_ui/templates/dashboard.html:73,80,81` → `url_for('repos_page')`, `url_for('api_keys_page')`, `url_for('repos_page')`.
- `src/web_ui/templates/ssh_keys.html:32,65` → `url_for('create_ssh_key')`, `url_for('delete_ssh_key', key_id=k.id)`.

### 4.5 JS fetches

`src/web_ui/templates/repos.html` ~4 sites use `fetch('/repos/jobs/.../status')` etc. JS can't call Jinja `url_for`. Pattern:

```html
<script>
  const ADMIN_PREFIX = "{{ request.scope.root_path }}";
  // fetch(`${ADMIN_PREFIX}/repos/jobs/${jobId}/status`)
</script>
```

Inject in `base.html` so all templates inherit. Edit JS sites in `repos.html` to use `ADMIN_PREFIX`.

### 4.6 `_LoopbackOnlyMiddleware`

No change. nginx proxies from `127.0.0.1`. Do NOT enable uvicorn `--proxy-headers` — it would surface real client IPs in `request.client.host` and break the loopback check. Add explicit regression test for this.

---

## 5. Test changes

### 5.1 Existing tests

All 6 webui test files (`tests/test_web_ui_*.py`) currently `client.get("/login")`, `client.get("/repos")`. After hard-coding `/admin`, every URL string in tests must become `/admin/login`, `/admin/repos`, etc. AsyncClient against the in-process app sees `root_path="/admin"` in scope; routes will only match prefixed URLs.

Mechanical sed across `tests/test_web_ui_*.py`:
- `/login` → `/admin/login`
- `/logout` → `/admin/logout`
- `/repos` → `/admin/repos` (careful: don't double-rewrite `/admin/repos`)
- `/api-keys` → `/admin/api-keys`
- `/ssh-keys` → `/admin/ssh-keys`
- Bare `/` (dashboard) → `/admin/`
- Assertions on redirect `Location` headers — update expected values.

### 5.2 Browser test fixture

`tests/test_web_ui_browser.py` uses Playwright + a uvicorn fixture (`web_ui_server` or similar). Update fixture base URL to `http://127.0.0.1:<port>/admin/`. Sub-page navigation assertions update similarly.

### 5.3 New regression test

Add `tests/test_web_ui_admin_prefix.py`:
- `GET /admin/login` → 200
- Unauthenticated `GET /admin/repos` → 302 `Location: /admin/login?next=%2Fadmin%2Frepos`
- Login POST → 302 `Location: /admin/`
- Logout → 302 `Location: /admin/login`
- Rendered HTML asserts `href="/admin/repos"`, NOT `href="/repos"`
- `safe_next` rejects `next=/etc/passwd`, `next=//evil.com`, `next=/repos` (missing prefix)
- Loopback middleware regression: send request with `X-Forwarded-For: 1.2.3.4` from `127.0.0.1` → still allowed (uvicorn `proxy-headers` off)

---

## 6. Nginx changes

File: `/etc/nginx/sites-enabled/odoo-semantic-mcp` AND mirror template `docs/deploy/nginx.conf.example`.

Add to the ` ssl` server block (mirror to any other `listen` blocks for the same hostname):

```nginx
location /admin/ {
    proxy_pass         http://127.0.0.1:8003;     # NO trailing slash; preserve URI
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
}
```

**Critical**: `proxy_pass http://127.0.0.1:8003;` (no trailing slash). With `root_path="/admin"` set on FastAPI, the app expects to receive `/admin/login` and internally strips `/admin` when matching routes. A trailing slash on `proxy_pass` (`.../8003/;`) strips `/admin` at the nginx layer — that's the buggy snippet currently in `docs/deploy.md:1023-1031`. Fix the doc.

No firewall changes needed (port 443 already public). No certbot changes (cert covers hostname for any port). No DNS changes.

systemd `webui.env` — no env var addition needed (prefix is hard-coded in code).

---

## 7. Documentation changes

| File | Change |
|------|--------|
| `docs/deploy.md` §11 (lines ~1015-1033) | Replace buggy `/admin/` snippet with the correct nginx block above. Remove stale "Web UI không có authentication" warning (M7 W16 added auth — ADR-0011). |
| `docs/deploy/nginx.conf.example` | Add the location block. |
| `docs/deploy/odoo-semantic-webui.service` | Comment block mentions UI lives at `/admin/`. No env-var change. |
| `README.md` | Local E2E Quickstart §4: change Web UI URL to `http://127.0.0.1:8003/admin/`. Trạng Thái section: add note about admin URL. |
| `~/install-odoo-semantic-mcp.md` (operator runbook on this machine — NOT in repo; per memory `install_runbook_location`) | Update web-UI verify step to `http://127.0.0.1:8003/admin/`. |
| `~/reindex-odoo-semantic-mcp.md` (per memory `reindex_runbook_location`) | No change unless it references webui URL — verify. |
| `CLAUDE.md` | Add a short line under "Dev Commands" noting webui base path is `/admin/`. |
| New `docs/adr/0012-webui-path-prefix.md` | ADR documenting: decision to hard-code `/admin`, refactor pattern (`root_path` + `url_for`), cookie-scope decision (try `path=/admin`, fallback `/`), dev URL impact, future scenario-A landing at `/` viability. |
| Pre-launch checklist `docs/deploy/pre-launch-checklist.md` | Add a row: "Web UI proxied at `/admin/` and IP-allowlist (or not) verified". |

---

## 8. Implementation order

Single PR, single commit (or 2-3 logical commits if pre-commit hooks complain about size). Recommended sequence inside the working branch:

1. Add `name=` to all 17 route decorators. Run `pytest tests/test_web_ui_` — should still pass (no behavioural change).
2. Wire `root_path` + cookie path on `app.py` + `__main__.py`. Tests will start failing because URLs are now `/admin/...` — that's expected.
3. Migrate all Python `RedirectResponse(...)` to `url_for`. Update `safe_next` validator + add open-redirect test.
4. Migrate templates to `url_for`. Inject `ADMIN_PREFIX` JS const in `base.html`.
5. Rewrite all test URL strings + browser fixture base URL.
6. Add `test_web_ui_admin_prefix.py` regression suite.
7. Update `docs/deploy.md`, `docs/deploy/nginx.conf.example`, `README.md`, `CLAUDE.md`.
8. Write `docs/adr/0012-webui-path-prefix.md`.
9. Update operator-local runbooks (`~/install-...`, `~/reindex-...`) — these are NOT in repo, do outside the PR.
10. Local validation: `make lint` + `make test` + `make test-integration`. Manual smoke: start webui, `curl http://127.0.0.1:8003/admin/login` → 200, `curl http://127.0.0.1:8003/login` → 404 (not 200 with old URL).
11. Open PR. Reviewer should verify: every `/login`/`/repos`/`/api-keys`/`/ssh-keys` string in code/tests is prefixed.

### Deployment after PR merge

1. `git pull --ff-only origin master` on host.
2. `~/.venv/odoo-semantic-mcp/bin/pip install -e ".[dev]"` (deps unchanged, but safe).
3. Drop the new nginx location block into `/etc/nginx/sites-enabled/odoo-semantic-mcp` (or run `install.sh --systemd` if it now ships the template — TBD, verify).
4. `sudo nginx -t && sudo systemctl reload nginx`.
5. `sudo systemctl restart odoo-semantic-webui`.
6. Smoke: `curl -sI https://odoo-semantic.viindoo.com/admin/login` → 200; with cookie verify login flow end-to-end from a browser on another network.

---

## 9. Risk & rollback

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Open-redirect via `?next=` after prefix change | Med | Tighten `safe_next` to require `startswith("/admin/")` + reject `//`. New unit test covers this. |
| `SessionMiddleware` doesn't accept `path=` kwarg | Low-Med | Fall back to `path="/"`. Cookie name `osm_session` doesn't collide with MCP (MCP doesn't read cookies). Document in ADR. |
| JS `fetch()` URLs miss prefix → 404 in repos UI | Med | Inject `ADMIN_PREFIX` const in `base.html`. Manual smoke must click through "Index repo" + watch job status update. Add browser test if budget allows. |
| Dev URL change breaks operator muscle memory | High (cosmetic) | Update runbook + README + CLAUDE.md. |
| Pre-existing browser tests fail under prefix | High if missed | Update `web_ui_server` fixture FIRST, run browser test locally before opening PR. |
| `_LoopbackOnlyMiddleware` triggers on real client IP if uvicorn picks up `X-Forwarded-For` | Low | Don't enable `--proxy-headers` on uvicorn. Regression test asserts nginx-proxied request still appears as `127.0.0.1`. |

### Rollback

1. Revert the PR via `gh pr revert <N>` → master returns to pre-prefix state.
2. Remove the `location /admin/` block from nginx config + `nginx -t && reload`.
3. `systemctl restart odoo-semantic-webui`.
4. Web UI back at `:8003/` (LAN-only, as before). Public access lost until re-deploy.

Sessions issued under `/admin` cookie path will be invalidated on rollback; users re-login.

---

## 10. Effort estimate

- Files touched: **~20** (12 source + 7 doc/deploy + 1 new test + 1 new ADR).
- LOC delta: **~+450 / -130** (templates + tests + ADR contribute most).
- Wall-clock: **4-6 hours** experienced engineer + nginx smoke. Add 1-2 hours if `SessionMiddleware(path=...)` requires a fallback patch.

---

## 11. Open questions

None at planning time — user has decided:
- Hard-code `/admin` (no env var).
- Cookie path: try `/admin`, fallback `/`.
- Plan-only first; code in subsequent session.
- Future `/` landing site deferred; scenario A nginx-static confirmed feasible.

Reviewer / next-session implementer: re-verify line numbers in §4 before editing; repo may have drifted since 2026-05-11.

---

## 12. Acceptance criteria (for the future PR)

- [ ] All 17 routes have `name=`.
- [ ] No literal `"/login"`/`"/repos"`/`"/api-keys"`/`"/ssh-keys"` string left in `src/web_ui/routes/`, `src/web_ui/middleware.py`, or `src/web_ui/templates/`.
- [ ] `make lint` clean.
- [ ] `make test` + `make test-integration` green.
- [ ] New `test_web_ui_admin_prefix.py` covers redirect chain + open-redirect guard.
- [ ] `docs/deploy.md` §11 snippet fixed (no trailing slash on `proxy_pass`).
- [ ] ADR-0012 written.
- [ ] CHANGELOG / README URL table updated.
- [ ] Manual smoke: from browser on another network, `https://odoo-semantic.viindoo.com/admin/` → login page → POST → dashboard → logout → 302 to login. No 404, no 401.
