# Milestone 8 — "Public Wow" ⚠️ SUPERSEDED

> **Status:** ⊘ SUPERSEDED — replaced by 2026-05-12-milestone-8-astro-unified.md

> **SUPERSEDED 2026-05-12** — Architecture revised to Astro unified (landing + admin) with FastAPI pure JSON API.
> See authoritative plan: [`2026-05-12-milestone-8-astro-unified.md`](2026-05-12-milestone-8-astro-unified.md)
>
> This file is kept for historical reference only. Do NOT implement from this file.

---

# Milestone 8 — "Public Wow" (original 2026-05-11 plan — HISTORICAL)

**Status:** Planning (plan-only, no code yet) — **SUPERSEDED**
**Created:** 2026-05-11
**Operator:** Viindoo team

---

## 1. Intent

Open the production host (`odoo-semantic.viindoo.com`) to **anonymous public traffic** with a polished landing site, while keeping admin operations safely gated. Set foundation for future SaaS productization (monthly subscription, public signup, billing).

## 2. Outcome — what changes for the visitor

| URL | Today | After M8 |
|-----|-------|----------|
| `https://odoo-semantic.viindoo.com/` | 404 | Public marketing + docs landing page with animated React Flow hero showing inheritance graph (cinematic, auto-revealing) |
| `https://odoo-semantic.viindoo.com/mcp` | MCP JSON-RPC endpoint (X-API-Key) | unchanged |
| `https://odoo-semantic.viindoo.com/install/` | MCP install onboarding HTML | unchanged (linked from landing CTA) |
| `https://odoo-semantic.viindoo.com/health` | MCP `/health` | unchanged |
| `https://odoo-semantic.viindoo.com/api/feedback*` | Feedback API | unchanged |
| `https://odoo-semantic.viindoo.com/docs/...` | n/a | Public docs (existing markdown rendered via Astro) |
| `https://odoo-semantic.viindoo.com/admin/` | n/a (webui is LAN-only) | Web UI proxied; session-auth required |

End user benefit: visitors see a real demonstration of the MCP's graph knowledge within 5 seconds of landing, with clear install CTA and docs deep-links.

Admin benefit: Web UI now reachable from outside LAN via standard browser session auth.

## 3. Scope decomposition

M8 is delivered as **two streams** that converge at the nginx integration step:

### Stream A — Web UI under `/admin/` (plan already written)

Detailed plan: [`docs/superpowers/plans/2026-05-11-webui-admin-prefix.md`](2026-05-11-webui-admin-prefix.md).

Key points (do not duplicate here):
- Hard-code `/admin` prefix (no env var) — user-locked decision.
- Pattern: FastAPI `root_path` + named routes + `request.url_for()` + Jinja `url_for`.
- Session cookie: try `path=/admin`, fallback `/`.
- ~20 files touched, +450/-130 LOC, 4-6h effort.
- Includes ADR-0012.

### Stream B — Public landing site (Astro + React Flow + baked graph snapshot)

User-locked decisions (2026-05-11):
- **SSG: Astro** — adds Node 20+ + npm to repo (one-time toolchain cost).
- **Hero animation: React Flow + cinematic mode** — declarative, marketer-editable, a11y baked in, ~100 kB hydration. Reusable in future authenticated dashboard at ≤1k node scale.
- **Data source: baked snapshot JSON** — `scripts/dump_graph_snippet.py` queries Neo4j once and writes `landing/public/graphs/sale-order.json`. No new public REST endpoint; no anonymous auth carve-out.

### Stream C — nginx integration (after A and B are mergeable)

Single nginx vhost edit landing both streams:
- `location /admin/` proxies to `127.0.0.1:8003` (Stream A).
- `location /` serves `/var/www/odoo-semantic-landing/` static files (Stream B).
- Existing `location /mcp`, `/health`, `/install` unchanged.

---

## 4. Stream B — landing site implementation plan (detailed)

### 4.1 Directory layout

```
landing/                              # new top-level dir (NOT under src/)
├── astro.config.mjs                  # Astro config
├── package.json                      # Node deps
├── pnpm-lock.yaml                    # lockfile (pnpm chosen for speed; npm acceptable)
├── tsconfig.json                     # strict TS
├── public/
│   ├── favicon.svg
│   ├── og-image.png                  # social share card
│   └── graphs/
│       └── sale-order.json           # baked snapshot from dump_graph_snippet.py
├── src/
│   ├── components/
│   │   ├── Hero.astro                # landing hero, embeds <GraphAnimation/> island
│   │   ├── GraphAnimation.tsx        # React island, React Flow cinematic
│   │   ├── InstallSnippets.astro     # 5-client install tabs (Claude/Codex/Gemini/VSCode/Antigravity)
│   │   ├── Pricing.astro             # placeholder pricing section (future SaaS)
│   │   └── Footer.astro
│   ├── content/
│   │   ├── config.ts                 # Astro content collection schema
│   │   └── docs/                     # markdown imported FROM repo docs/ (build step)
│   ├── layouts/
│   │   └── Base.astro
│   ├── pages/
│   │   ├── index.astro               # /
│   │   ├── docs/[...slug].astro      # /docs/* dynamic route
│   │   └── pricing.astro             # /pricing
│   └── styles/
│       └── global.css
└── README.md                         # landing dev workflow
```

### 4.2 Astro setup decisions

- **Adapter**: `@astrojs/static` — pure SSG, output `landing/dist/` rsynced to `/var/www/odoo-semantic-landing/`.
- **React integration**: `@astrojs/react` for the GraphAnimation island only. Everything else is `.astro` (no hydration).
- **Image optimization**: `@astrojs/image` with sharp.
- **MDX**: `@astrojs/mdx` for docs collection.
- **i18n**: deferred to follow-up — landing ships English first, VI later. Both languages exist in `docs/`; the build step maps `docs/*-vn.md` → `/docs/vi/*`, others → `/docs/en/*`.

### 4.3 Content reuse from `docs/`

A small Node script `landing/scripts/import-docs.mjs` copies/symlinks selected markdown from repo `docs/` into `landing/src/content/docs/` at build time:

| Source | Destination route |
|--------|------------------|
| `docs/client-setup.md` | `/docs/client-setup` |
| `docs/deploy.md` | `/docs/deploy` (admin-targeted, behind a "Admins" tab) |
| `docs/huong-dan-stack.md` | `/docs/stack-guide-vi` |
| `docs/thiet-ke-kien-truc.md` | `/docs/architecture-vi` |
| Selected ADRs (0001, 0007, 0011, 0012) | `/docs/adr/<id>` |

`README.md` is NOT copied — landing replaces its public role.

### 4.4 Hero — React Flow cinematic mode

Story: a visitor lands → after ~600ms the inheritance chain `sale.order` autoplays:
1. Frame 1: `(:Model {name: 'sale.order', module: 'sale'})` fades in center.
2. Frame 2: `(:Module {name: 'viin_sale'})` slides in, edge `INHERITS` draws.
3. Frame 3: Two more module nodes slide in (`(...module + another)`), each connected.
4. Frame 4: Field count badge appears on each Model node ("148 fields").
5. Frame 5: Subtle pulse on a method override edge (`OVERRIDES`).
6. End state: graph holds; user can drag/zoom IF they hover (optional, default off).

Component spec `landing/src/components/GraphAnimation.tsx`:
- Loads `/graphs/sale-order.json` at hydration.
- Uses `@xyflow/react` v12 (latest at 2026-05-11) with custom node types (`ModelNode`, `ModuleNode`).
- Layout: precomputed `x`/`y` in the JSON (no runtime layout engine — keeps payload + CPU low).
- Animation: `framer-motion` for entry tweens; React Flow's built-in `fitView` for camera.
- Interactivity toggle: hidden by default; "Click to explore →" button enables drag/zoom + reveals controls panel.
- Loaded with `client:visible` directive — does not block first paint.

Fallback (no-JS, screen readers): `<noscript>` + `<details>` containing the same graph as a nested `<ul>` tree. Crawlers index that.

### 4.5 Baked snapshot — `scripts/dump_graph_snippet.py`

New Python script in repo root `scripts/` dir:

```python
# Pseudo — actual implementation in M8 PR
"""Dump a small graph snippet from Neo4j for the landing hero.

Reads NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD from .env.
Queries: MATCH (m:Model {name:'sale.order'})<-[:INHERITS*0..2]-(...) ...
Writes: landing/public/graphs/sale-order.json with precomputed x/y positions.

Run manually after each major reindex. Idempotent — same query always emits same JSON modulo timestamps.
"""
```

Output JSON shape (React Flow compatible):
```json
{
  "nodes": [
    {"id": "sale__sale.order", "type": "model", "position": {"x": 0, "y": 0},
     "data": {"name": "sale.order", "module": "sale", "fields": 148, "is_definition": true}},
    {"id": "viin_sale__sale.order", "type": "model", "position": {"x": 200, "y": 0},
     "data": {"name": "sale.order", "module": "viin_sale", "fields": 12, "is_definition": false}}
  ],
  "edges": [
    {"id": "e1", "source": "viin_sale__sale.order", "target": "sale__sale.order",
     "type": "inherits", "animated": true, "label": "INHERITS"}
  ]
}
```

Position assignment: simple radial-tree heuristic in the script (root at center, depth-1 children at angle steps). Manual override allowed via `--pin <node>=<x>,<y>` for marketing-tuned layout.

### 4.6 Pricing placeholder

`src/components/Pricing.astro` shows 3 tiers as a teaser (Free / Pro / Team) with "Coming soon — join waitlist" CTAs. No payment integration in M8. Waitlist form posts to a Formspree-equivalent or a new MCP endpoint `POST /api/waitlist` (defer — could be Google Form for v1).

### 4.7 Build artifact + deploy

- Local dev: `cd landing && pnpm dev` → http://127.0.0.1:4321/.
- Build: `cd landing && pnpm build` → `landing/dist/`.
- Deploy: `rsync -av --delete landing/dist/ /var/www/odoo-semantic-landing/`.
- CI: GitHub Actions `landing-build.yml` runs `pnpm build` on every PR touching `landing/**` or `docs/**`; uploads artifact for review.
- Production deploy: manual `make landing-deploy` target in the repo (calls rsync). NO auto-deploy on master push — operator gates.

### 4.8 Top-level repo changes

- Add `landing/` dir (all of §4.1 above).
- Add `scripts/dump_graph_snippet.py`.
- Add `Makefile` targets: `landing-dev`, `landing-build`, `landing-deploy`, `landing-snapshot`.
- Update `.gitignore`: `landing/node_modules/`, `landing/dist/`, `landing/.astro/`.
- Update `pyproject.toml`: no changes (script uses existing `neo4j` dep).
- Update CI workflow `.github/workflows/landing-build.yml` (new).
- Update `README.md`: replace install snippet section with link to `https://odoo-semantic.viindoo.com/`. Keep self-host quickstart in repo README.

---

## 5. Stream C — nginx integration

Single change to `/etc/nginx/sites-enabled/odoo-semantic-mcp` (and mirror template `docs/deploy/nginx.conf.example`):

```nginx
server {
    listen 9999 ssl http2;
    server_name odoo-semantic.viindoo.com;

    # ... existing ssl_certificate / ssl_protocols ...

    # NEW: public landing at root
    location / {
        root      /var/www/odoo-semantic-landing;
        try_files $uri $uri/ /index.html;
        # Cache static assets aggressively
        location ~* \.(css|js|svg|png|jpg|woff2)$ {
            expires 7d;
            add_header Cache-Control "public, immutable";
        }
    }

    # NEW: Web UI admin (Stream A)
    location /admin/ {
        proxy_pass         http://127.0.0.1:8003;     # no trailing slash
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # EXISTING (unchanged): /mcp /install /health /api
    # ... existing location blocks ...
}
```

nginx longest-prefix matching guarantees `/mcp`, `/install`, `/admin/`, `/health`, `/api` all win over `/`. Order in file doesn't matter for prefix locations.

Pre-deploy validation: `sudo nginx -t` before `systemctl reload nginx`.

Create `/var/www/odoo-semantic-landing/` with owner `<user>:www-data` (or whatever nginx runs as) — verify with `ls -la /var/www/`.

---

## 6. Implementation order

Multi-PR strategy (user pattern from earlier sessions: "split logical commits"):

| PR | Scope | Branch | Blockers |
|----|-------|--------|----------|
| **#A** | Stream A code-only (Web UI prefix migration + tests + ADR-0012) | `feat/m8-admin-prefix` | none — follows existing plan file |
| **#B** | Stream B Astro scaffold (landing/, package.json, basic index.astro, no animation yet) | `feat/m8-landing-scaffold` | none |
| **#C** | Stream B graph snippet script + JSON committed | `feat/m8-graph-snapshot` | #B |
| **#D** | Stream B React Flow hero component + content reuse from docs/ | `feat/m8-hero-animation` | #C |
| **#E** | Stream B docs pages + pricing placeholder + waitlist | `feat/m8-landing-content` | #D |
| **#F** | Stream C nginx config update + deploy.md doc fix + Makefile targets | `feat/m8-nginx-integration` | #A merged + #E merged |
| **#G** | CI workflow `landing-build.yml` | `feat/m8-ci-landing` | #B merged |

Each PR is independently reviewable and revertable. #F is the only deployment-affecting PR — `nginx -t` must pass, manual smoke through public URL.

Estimated effort:
- #A: 4-6h
- #B: 3-4h (Astro scaffold + theme decisions)
- #C: 2-3h (script + initial baked JSON)
- #D: 1-2 days (animation polish takes time)
- #E: 1 day (content tuning, docs migration)
- #F: 1-2h
- #G: 1-2h

**Total: ~3-4 working days for one engineer.**

---

## 7. Risk & rollback

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Astro Node toolchain bitrot (pnpm version mismatch, npm registry hiccups) | Med | Pin `engines.node` in `package.json`; lockfile committed; CI verifies on clean install. |
| React Flow API change between v12 minor versions | Low | Pin exact version in `package.json`; renovate-bot upgrades reviewed. |
| Graph snapshot drift — landing shows stale module count after major Odoo upgrade | Med | Operator runs `make landing-snapshot && make landing-deploy` after each `index-repo --full`. Add reminder to `docs/deploy.md`. |
| `_LoopbackOnlyMiddleware` regression from Stream A breaks Web UI behind nginx | Low | Stream A plan §9 covers; regression test asserts proxied request is 127.0.0.1. |
| nginx `location /` catches `/admin/foo` due to misconfig | Low | nginx config validated with `nginx -t`; integration smoke from public IP tests both URLs. |
| Public landing URL exposes secret in baked JSON (e.g. private module names) | Med | `dump_graph_snippet.py` filters to public Odoo CE modules only by default — `--include-private` flag for opt-in. PR review must verify no private data. |
| First-paint regression on mobile 3G — hero too heavy | Med | Lighthouse perf gate in CI on `landing-build`; fail if mobile perf < 80. |
| Future SaaS pivot breaks public URL contracts | Low | Pricing/waitlist routes are placeholders; signup/billing routes added in M9+, not M8. |

### Rollback

If something breaks publicly:
1. **nginx fast rollback**: revert PR #F + `sudo nginx -t && sudo systemctl reload nginx`. Public URL returns to 404. Admin UI returns to LAN-only.
2. **Code-only rollback** (admin prefix): `gh pr revert <#A>` → master back to LAN-only webui.
3. **Asset rollback**: keep last 3 deployed snapshots in `/var/www/odoo-semantic-landing-history/` (operator script). One-line `cp -r` swap.

---

## 8. SaaS roadmap implications (not in M8 scope, documented for future)

M8 sets foundation for monthly-subscription SaaS but does NOT ship billing/signup. Future milestones:

- **M9 "Auth Wow"**: public signup (email+password OR OAuth Google/GitHub), tenant-scoped API keys, tenant DB partition. ADR-0013.
- **M10 "Billing Wow"**: Stripe integration, plan tiers (Free / Pro / Team), usage metering on `/mcp` endpoint, dunning.
- **M11 "Dashboard Wow"**: authenticated `/dashboard` (separate from `/admin` which stays operator-only) where customers see their indexed repos + usage + billing. Reuses React Flow component from M8 hero for "my graph" view.
- **M12 "Multi-tenant Wow"**: Neo4j multi-database OR namespaced labels per tenant; cross-tenant isolation tests.

React Flow choice in M8 is deliberate: same component renders M11 dashboard at small scale (≤1k nodes per tenant). If we ever need 10k+ nodes per visualization, swap to Sigma.js + Graphology with same data model.

---

## 9. Open questions

None at planning time — all toolchain and design decisions locked by user 2026-05-11. Re-verify before PR #B opens:

- Pinned versions: `astro@^4`, `@astrojs/react@^3`, `@xyflow/react@^12`, `framer-motion@^11`.
- License audit: Astro (MIT), React Flow / @xyflow (MIT), framer-motion (MIT) — all compatible.
- Node version: 20 LTS pinned in `package.json` `engines`.

---

## 10. Acceptance criteria (for the M8 milestone close)

When all PRs #A through #G are merged AND deployed:

- [ ] `https://odoo-semantic.viindoo.com/` returns 200 with public landing HTML.
- [ ] Hero animation autoplays within 1s of page load, completes within 5s.
- [ ] Page Lighthouse mobile perf ≥ 80, accessibility ≥ 95, SEO ≥ 95.
- [ ] `<noscript>` fallback contains the graph as nested `<ul>` tree.
- [ ] `https://…/admin/login` returns 200 with session-auth login form.
- [ ] `https://…/mcp`, `/health`, `/install/`, `/api/feedback` unchanged.
- [ ] `nginx -t` passes; `systemctl reload nginx` clean.
- [ ] All existing tests + new admin-prefix regression test green.
- [ ] CI `landing-build.yml` green.
- [ ] ADR-0012 (admin prefix) + ADR-0013 (landing site decisions) committed.
- [ ] `docs/deploy.md` snippet fix landed.
- [ ] `~/install-odoo-semantic-mcp.md` operator runbook updated (out-of-repo, manual).
- [ ] `make landing-snapshot && make landing-deploy` documented + smoke-tested.

---

## 11. Reviewer / next-session instructions

A future Claude session (or human implementer) executing M8:

1. Start with Stream A — `docs/superpowers/plans/2026-05-11-webui-admin-prefix.md` is self-contained. Verify line numbers haven't drifted before editing.
2. After Stream A merged, run a single Sonnet investigator to confirm Astro + React Flow version pins are still current (the ecosystem moves fast).
3. Stream B PR sequence (B → C → D → E) can be done in parallel by separate sub-agents per the orchestrated-workflow pattern (CLAUDE.md §"Orchestrated Multi-Subagent Workflow"). Each PR is independent off master.
4. Stream C (#F) gates on both A AND E merging. Do NOT merge #F until manual smoke from public IP confirms both URLs.
5. Operator-side: update `~/install-odoo-semantic-mcp.md` runbook with new `/admin/` URL after #A deploys; add `make landing-deploy` to deploy runbook after #F.

ADR-0013 to write at the start of Stream B: "Astro for public site + React Flow for graph viz; baked JSON snapshot; deferred SaaS billing to M9-M11."
