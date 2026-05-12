# Milestone 8 — "Public Wow" (Astro Unified — Revised Plan)

**Status:** Planning (revised 2026-05-12, no code yet)
**Created:** 2026-05-12
**Supersedes:** `2026-05-11-milestone-8-public-wow.md` + `2026-05-11-webui-admin-prefix.md`

---

## 1. Intent

Open production host to anonymous traffic with a polished landing site **and** a fully Astro-based admin UI. Jinja2 is removed entirely in M8. FastAPI becomes a pure JSON API backend.

---

## 2. Architecture Change (vs. prior plan)

### Old (2026-05-11 plan)
```
nginx
├── /          → static files (Astro SSG landing, landing/ dir)
├── /admin/    → FastAPI port 8003 (Jinja2 SSR, kept intact)
└── /mcp       → FastAPI port 8002 (unchanged)
```

### New (2026-05-12 — Astro unified)
```
nginx (port 9999, prod)
├── /          → Astro server port 4321 (static landing)
├── /admin/*   → Astro server port 4321 (SSR admin, auth-gated)
├── /api/*     → FastAPI port 8003 (JSON API only, no Jinja2)
├── /mcp       → FastAPI port 8002 (unchanged)
└── /install/, /health → FastAPI port 8002 (unchanged)
```

**Session flow:**
1. `/admin/*` request → Astro middleware → `GET /api/auth/verify` → FastAPI
2. FastAPI checks signed cookie (bcrypt cost=12, 8h TTL, SameSite=strict, HttpOnly) → 200 or 401
3. 401 → Astro redirects to `/admin/login`
4. `/admin/login` submits `POST /api/auth/login` (JSON) → FastAPI sets cookie → Astro page continues

Dev: `astro.config.mjs` proxy `/api/*` → `http://localhost:8003` (same-origin cookies in dev).

---

## 3. Decisions Locked

| Date | Decision |
|------|---------|
| 2026-05-11 | Astro (Node 20+ LTS), React Flow + cinematic mode, baked JSON snapshot, pnpm |
| 2026-05-11 | React Flow `@xyflow/react` v12, framer-motion v11 |
| 2026-05-12 | Astro `output: 'hybrid'` (static landing + SSR admin in one Astro server) |
| 2026-05-12 | Tailwind CSS (`@astrojs/tailwind`) for all pages and React islands |
| 2026-05-12 | `site/` top-level dir (replaces `landing/` from old plan) |
| 2026-05-12 | FastAPI → pure JSON API; Jinja2 removed completely in M8 |
| 2026-05-12 | LoopbackOnlyMiddleware kept (Astro loopback too → compatible) |
| 2026-05-12 | SessionMiddleware + bcrypt kept in FastAPI (no migration to Astro) |
| 2026-05-12 | ADR-0012: Astro unified; ADR-0013: FastAPI pure JSON API |

---

## 4. Streams

M8 is 4 streams. W1+W2+W4 are independent (parallel dispatch). W3 depends on W1+W2 (synthetic base). W5 depends on W2. W6 depends on W3+W5.

### Worktree topology
```
master ┬── W1 (FastAPI JSON API) ─────────────────────────┐
       ├── W2 (Astro scaffold) ──── W5 (landing + hero)   ├── W3 (admin pages) ── W6 (systemd + CI)
       │                       └──────────────────────────┘
       └── W4 (nginx config)        (W3 synthetic base = master + W1 + W2)
                                    (W6 synthetic base = master + W3 + W5)
```

---

### Stream A — FastAPI Pure JSON API

**Branch:** `feat/m8-fastapi-json-api`
**Effort:** ~3-4h
**Agent:** Sonnet (auth patterns, multiple file changes)

**Delete:**
- `src/web_ui/templates/` — all 7 Jinja2 templates (990 LOC)
- `AuthRequiredMiddleware` HTML redirect behavior → replace with 401 JSON
- `FastAPI(root_path="/admin")` (not needed; prefix is Astro's job)
- `jinja2` from `pyproject.toml` dependencies

**Add (new endpoints):**
- `POST /api/auth/login` — JSON body `{username, password}` → set session cookie → `{"ok": true}`
- `POST /api/auth/logout` — clear cookie → `{"ok": true}`
- `GET /api/auth/verify` — check session cookie → 200 `{"username": "..."}` or 401 `{"error": "unauthenticated"}`

**Convert all existing routes to `/api/*` prefix + JSON responses:**
- `/api/repos/profiles` (GET list, POST create, DELETE)
- `/api/repos/repos` (GET list, POST create, DELETE)
- `/api/repos/repos/{id}/index` (POST, async job start)
- `/api/repos/repos/{id}/clone-status` (GET JSON)
- `/api/repos/repos/{id}/reset-embed` (POST)
- `/api/repos/ssh-keys-list` (GET JSON)
- `/api/api-keys` (GET list, POST create)
- `/api/api-keys/{id}/deactivate` (POST)
- `/api/ssh-keys` (GET list, POST create)
- `/api/ssh-keys/{id}/delete` (POST)
- `/api/operations/index-core` (POST)
- `/api/operations/seed-patterns` (POST)
- `/api/operations/apply-preset` (POST)
- `/api/feedback` (POST, GET `/{id}`) — already JSON, just prefix update
- `/api/jobs/{id}/status` (GET) — existing job tracking endpoint

**Keep unchanged:**
- `LoopbackOnlyMiddleware` (Astro server is also loopback → compatible)
- `SessionMiddleware` + bcrypt + `webui_users` table + `create-webui-user` CLI
- `WEBUI_SESSION_SECRET`, `WEBUI_SECURE_COOKIE=0` dev flag
- Port 8003 binding (loopback only, per ADR-0011)

**Files touched:** `src/web_ui/app.py`, `src/web_ui/routes/*.py` (7 files), `src/web_ui/auth.py`, `src/web_ui/middleware.py`, `pyproject.toml`, `tests/test_web_ui*.py` (JSON assertions instead of HTML)

**ADR to write:** `docs/adr/0013-fastapi-pure-json-api.md` — FastAPI JSON API only; Jinja2 removal; session auth proxy pattern (Astro calls `/api/auth/verify`).

---

### Stream B — Astro Hybrid Full

**Branches:** W2 scaffold → W5 landing → (W3 admin pages, synthetic base off W1+W2)
**Effort:** ~4-5 days total
**Agents:** Sonnet for W2+W3, Sonnet for W5

#### W2 — Astro Scaffold (`feat/m8-astro-scaffold`, ~3-4h)

```
site/
├── astro.config.mjs    ← output: 'hybrid', react(), tailwind(), proxy /api/* → localhost:8003
├── package.json        ← @astrojs/react, @astrojs/tailwind, @xyflow/react ^12, framer-motion ^11
├── pnpm-lock.yaml
├── tsconfig.json       ← strict TS
├── .gitignore          ← node_modules/, dist/, .astro/
├── public/
│   ├── favicon.svg
│   └── og-image.png
└── src/
    ├── middleware.ts   ← /admin/* auth guard (fetch /api/auth/verify)
    ├── layouts/
    │   ├── BaseLayout.astro    ← public pages (head, meta, minimal nav)
    │   └── AdminLayout.astro   ← admin sidebar + topbar + Tailwind theme
    └── styles/
        └── global.css          ← Tailwind directives
```

Astro config key decisions:
- `output: 'hybrid'` (mix static + SSR)
- `adapter: '@astrojs/node'` (standalone mode for SSR)
- Dev proxy: `vite.server.proxy = { '/api': 'http://localhost:8003' }`
- Static pages: `export const prerender = true` (index.astro, pricing.astro)
- SSR pages: default (admin/*)

#### W3 — Admin Pages (`feat/m8-admin-pages`, ~2-3 days, synthetic base W1+W2)

7 admin pages porting Jinja2 → Astro SSR:

```
site/src/pages/admin/
├── index.astro        ← dashboard (stats: profiles, repos, embeddings counts)
├── repos.astro        ← profile + repo CRUD (list, add, delete, index)
├── api-keys.astro     ← API key management
├── ssh-keys.astro     ← SSH key upload + management
├── operations.astro   ← index-core, seed-patterns, apply-preset, job status
└── login.astro        ← login form (SSR, no auth guard)
```

```
site/src/components/
├── StatsCard.astro    ← reusable stats card (dashboard)
├── RepoTable.astro    ← repo list rows (static Astro, interactive in M9)
└── JobStatus.astro    ← polling component for long-running ops
```

Pattern for each admin page:
1. Server-side `fetch('/api/...')` in frontmatter (SSR data fetch)
2. Render with Tailwind-styled Astro template
3. Forms submit via JS `fetch POST /api/...` → JSON response → client-side state update (no full page reload)
4. Long-running ops (index-repo, etc.): poll `/api/jobs/{id}/status` every 2s with client JS

Tailwind admin theme: dark sidebar, white main area. Consistent with React components.

Middleware `src/middleware.ts`:
```typescript
// Pseudo — actual in WI
import { defineMiddleware } from 'astro:middleware';

export const onRequest = defineMiddleware(async (context, next) => {
  if (!context.url.pathname.startsWith('/admin/')) return next();
  if (context.url.pathname === '/admin/login') return next(); // exempt

  const verify = await fetch(`http://localhost:8003/api/auth/verify`, {
    headers: { cookie: context.request.headers.get('cookie') ?? '' }
  });

  if (!verify.ok) {
    return context.redirect('/admin/login');
  }
  return next();
});
```

#### W5 — Landing Pages + Hero (`feat/m8-landing-content`, ~1-2 days, off W2)

```
site/src/pages/
├── index.astro         ← export const prerender = true; GraphHero island; CTA → /install/
├── pricing.astro       ← export const prerender = true; 3 tiers teaser

site/src/components/
├── GraphHero.tsx       ← React Flow island (client:visible); loads graph-snapshot.json
└── InstallSnippets.astro ← 5-client tabs (Claude/Codex/Gemini/VSCode/Antigravity)

site/public/
└── graph-snapshot.json  ← output of scripts/dump_graph_snippet.py
```

GraphHero cinematic spec (preserved from old plan):
- Frame 1: `(:Model {sale.order, module:'sale'})` fades in.
- Frame 2: `(:Model {sale.order, module:'viin_sale'})` slides in, INHERITS edge draws.
- Frame 3: Two more module nodes reveal.
- Frame 4: Field count badge ("148 fields") appears.
- Frame 5: Method override edge pulses.
- End: hold; optional drag/zoom on hover.
- `<noscript>` fallback: `<ul>` tree (SEO + a11y).

`scripts/dump_graph_snippet.py` (new script, repo root):
- Queries Neo4j: `MATCH (m:Model {name:'sale.order'})<-[:INHERITS*0..2]-(...)`
- Output: `site/public/graph-snapshot.json` (React Flow node/edge format with precomputed x/y)
- Idempotent; `--include-private` flag opt-in.

ADR to write: `docs/adr/0012-astro-unified.md` — Astro output:hybrid; islands architecture; React Flow for hero + M11 dashboard reuse; Tailwind; baked JSON snapshot; deferred SaaS billing to M9+.

---

### Stream C — nginx Integration

**Branch:** `feat/m8-nginx-integration`
**Effort:** ~1-2h
**Agent:** Haiku
**Gate:** Stream A merged + Stream B (W3+W5) merged

```nginx
server {
    listen 9999 ssl http2;
    server_name odoo-semantic.viindoo.com;
    # ... existing ssl_certificate / ssl_protocols ...

    # NEW: FastAPI JSON API
    location /api/ {
        proxy_pass         http://127.0.0.1:8003;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # NEW: Astro server (landing static + admin SSR) — replaces old /admin/ → :8003
    location / {
        proxy_pass         http://127.0.0.1:4321;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        "upgrade";
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    # EXISTING (unchanged): /mcp /install/ /health
    location /mcp { ... }
    location /install/ { ... }
    location /health { ... }
}
```

nginx prefix matching: `/api/`, `/mcp`, `/install/`, `/health` all win over `/` (longest prefix wins).

**REMOVE** from existing nginx config: `location /admin/` (old proxy to :8003 static files).
`/var/www/odoo-semantic-landing/` dir no longer needed (Astro server replaces static files).

---

### Stream D — systemd + CI

**Branch:** `feat/m8-astro-service`
**Effort:** ~4-6h
**Agent:** Haiku
**Gate:** Stream B fully merged

**New systemd unit `docs/deploy/odoo-semantic-astro.service`:**
```ini
[Unit]
Description=Odoo Semantic MCP — Astro frontend server
After=network.target odoo-semantic-webui.service

[Service]
Type=simple
User=odoo-semantic
WorkingDirectory=/opt/odoo-semantic-mcp/site
ExecStart=/usr/bin/node dist/server/entry.mjs
Restart=on-failure
RestartSec=5
Environment=HOST=127.0.0.1
Environment=PORT=4321
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Deploy flow:
```bash
cd /opt/odoo-semantic-mcp/site
pnpm run build   # → dist/
sudo systemctl restart odoo-semantic-astro
```

**CI `ci.yml` additions:**
```yaml
- uses: actions/setup-node@v4
  with:
    node-version: '20'
    cache: 'pnpm'

- run: pnpm install --frozen-lockfile
  working-directory: site

- run: pnpm run check   # Astro typecheck
  working-directory: site

- run: pnpm run build   # production build smoke
  working-directory: site
```

**Browser tests:** Update base URL from `/login` → `/admin/login`; `/repos` → `/admin/repos`; etc.

**`nightly-smoke.yml`:** Add smoke checks:
- `GET /` → 200 (Astro landing)
- `GET /admin/login` → 200 (Astro SSR)
- `GET /api/auth/verify` → 401 JSON (no cookie)

---

## 5. Implementation Order (Multi-Subagent)

| WI | Branch | Gate | Agent | Effort |
|----|--------|------|-------|--------|
| W1 (FastAPI JSON API) | `feat/m8-fastapi-json-api` | none | Sonnet | 3-4h |
| W2 (Astro scaffold) | `feat/m8-astro-scaffold` | none | Sonnet | 3-4h |
| W4 (nginx) | `feat/m8-nginx-integration` | W1+W3+W5 merged | Haiku | 1-2h |
| W5 (landing + hero) | `feat/m8-landing-content` | W2 | Sonnet | 1-2 days |
| W3 (admin pages) | `feat/m8-admin-pages` | W1+W2 (synthetic base) | Sonnet | 2-3 days |
| W6 (systemd + CI) | `feat/m8-astro-service` | W3+W5 (synthetic base) | Haiku | 4-6h |

W1 + W2 dispatch parallel in one message. W5 dispatched after W2 lands. W3 dispatched after W1+W2 land (via synthetic base). W6 last.

---

## 6. ADRs to Write

- **ADR-0012:** `docs/adr/0012-astro-unified.md` — Astro `output: 'hybrid'` for unified landing + admin; islands architecture; React Flow reuse plan (M8 hero → M11 dashboard); Tailwind; baked JSON snapshot rationale.
- **ADR-0013:** `docs/adr/0013-fastapi-pure-json-api.md` — Jinja2 removal; FastAPI → pure JSON API; session auth proxy pattern (Astro middleware calls `/api/auth/verify`); LoopbackOnlyMiddleware compatibility.

---

## 7. Risk & Rollback

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Astro Node toolchain bitrot | Med | Pin `engines.node: '>=20'` in package.json; pnpm lockfile committed; CI verifies on clean install |
| Cookie domain mismatch (Astro dev vs FastAPI) | Med | Astro vite proxy `/api/*` → localhost:8003 ensures same-origin in dev |
| Session cookie not forwarded from Astro → FastAPI | Med | Middleware must forward `cookie` header verbatim; test with curl |
| LoopbackOnlyMiddleware rejecting Astro | Low | Astro SSR runs on same host → request is from 127.0.0.1 → passes |
| Jinja2 removal breaks test assertions | Med | All `test_web_ui*.py` switch to JSON assertions before Jinja2 removed |
| Graph snapshot exposes private module data | Med | `dump_graph_snippet.py` filters CE-only by default; `--include-private` opt-in |
| Lighthouse perf regression | Med | CI gate: pnpm build + Lighthouse check; fail if mobile perf < 80 |
| nginx `location /` catches `/api/` or `/mcp/` | Low | nginx longest-prefix matching; `/api/`, `/mcp`, `/install/`, `/health` win over `/` |

**Rollback:**
1. nginx: revert Stream C PR + reload nginx → Astro server no longer public.
2. FastAPI: revert Stream A PR → Jinja2 routes back (requires backup of templates dir).
3. Keep last 2 `graph-snapshot.json` commits for quick `git revert` of baked data.

---

## 8. Documents to Update After M8

- `README.md`: Local quickstart — add `pnpm run dev` in `site/` dir; update admin URL to `/admin/`; add Node 20 requirement.
- `CONTRIBUTING.md`: Add Node 20 + pnpm prerequisites; `pnpm run dev` + `pnpm run check` steps.
- `docs/deploy.md`: nginx config rewrite (Astro server); new systemd unit; remove static file rsync.
- `docs/deploy/pre-launch-checklist.md`: Update `/login` → `/admin/login`; add Astro service start check.
- `CLAUDE.md`: ADR list entry for 0012 + 0013.
- `~/install-odoo-semantic-mcp.md` (out-of-repo runbook): Update port + URL after deployment.

---

## 9. Acceptance Criteria

- [ ] `GET /` → 200, Lighthouse mobile perf ≥ 80, accessibility ≥ 95, SEO ≥ 95
- [ ] Hero animation autoplays within 1s, completes within 5s
- [ ] `<noscript>` fallback contains graph as `<ul>` tree
- [ ] `GET /admin/login` → 200 (Astro SSR, no auth required)
- [ ] `POST /api/auth/login` correct creds → 200 JSON `{"ok": true}` + `Set-Cookie`
- [ ] `POST /api/auth/login` wrong creds → 401 JSON `{"error": "..."}`
- [ ] `GET /admin/` unauthenticated → redirect 302 → `/admin/login`
- [ ] `GET /admin/` authenticated → 200 dashboard with real counts
- [ ] `GET /api/repos/profiles` no cookie → 401 JSON
- [ ] `GET /mcp`, `/install/`, `/health` → unchanged (no regression)
- [ ] Jinja2 not in `pyproject.toml` dependencies
- [ ] `python -c "import src.web_ui.app"` does not import jinja2
- [ ] `nginx -t` passes
- [ ] `make lint && make test` green
- [ ] `pnpm run check` (Astro typecheck) green in `site/`
- [ ] `odoo-semantic-astro.service` starts + survives `systemctl restart`
- [ ] ADR-0012 + ADR-0013 committed
- [ ] `docs/deploy.md` updated with new nginx config
