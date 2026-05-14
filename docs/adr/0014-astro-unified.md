# ADR-0014: Astro Unified Architecture for Landing + Admin

**Status:** Accepted
**Date:** 2026-05-14
**Author:** M8 W3 subagent

---

## Context

Through M7.5 the Web UI was implemented as Jinja2 templates rendered by FastAPI (`src/web_ui/`). This served internal admin needs adequately but created two problems as M8 targets a public launch:

1. **Single origin, mixed concerns.** Jinja2 templates and the JSON API share the same FastAPI process, making it harder to independently scale or cache the marketing landing pages.
2. **No unified stack for landing + admin.** Marketing pages (hero, pricing, persona guides) and admin pages (repos, API keys, operations) had no common component model. Adding a polished public landing required either a separate static site or bolting another templating layer onto FastAPI.

M8 "Public Wow" requires:
- A production-quality landing page for potential users
- Admin pages rebuilt with consistent Tailwind-based design system
- A foundation for React islands in M11 (dashboard charts, hero animations)
- Zero-downtime deploys and straightforward Caddy/Nginx proxy configuration

---

## Decision

Adopt **Astro 5.x** as the unified frontend runtime for both the public landing site and the admin panel.

### Key choices

| Concern | Decision | Rationale |
|---------|----------|-----------|
| Output mode | `output: 'server'` (SSR by default; Astro 5.x merged `'hybrid'` into `'server'`) | Lets per-page `export const prerender = true` opt static pages out of SSR while admin pages stay dynamic |
| Static opt-out | `export const prerender = true` per file | Landing pages (M5) are static; admin pages are always dynamic (SSR fetch from FastAPI) |
| Adapter | `@astrojs/node` in `standalone` mode | Single Node process; compatible with systemd + Caddy reverse proxy; no serverless runtime coupling |
| Package manager | `pnpm` | Consistent with Viindoo toolchain; workspace support for future multi-package layout |
| CSS | Tailwind CSS via `@astrojs/tailwind` | Utility-first; no runtime CSS-in-JS; pairs well with Astro's zero-JS-by-default philosophy |
| React islands | `@astrojs/react` present but inactive | Hero animation + dashboard charts planned for M11; React components will be added incrementally as `client:load` islands without refactoring page shells |
| API backend | FastAPI (`src/web_ui/`) serves pure JSON on `:8003` | Decoupled: Astro on `:4321` proxies `/api/*` to `:8003` in dev; Caddy routes in prod |
| Auth | Astro middleware calls `GET /api/auth/verify` cookie-forward | Session cookie issued by FastAPI (ADR-0011 bcrypt/session); middleware redirects unauthed requests to `/admin/login` |

### Baked JSON snapshot for hero

The hero section will use a static JSON snapshot of representative query results (committed to `site/public/graph-snapshot.json`, served as-is without bundler indirection) rather than a live `resolve_model` call. This avoids exposing a public Neo4j read endpoint and keeps the landing page fully prerenderable.

---

## Architecture overview

```
Browser
  └── Caddy (TLS termination + routing)
        ├── /api/*       → FastAPI :8003 (pure JSON API, ADR-0015)
        ├── /admin/*     → Astro :4321 (SSR admin pages — W3)
        └── /*           → Astro :4321 (static landing pages — W5)

Astro :4321
  ├── src/pages/index.astro            (prerender=true — landing)
  ├── src/pages/admin/*.astro          (SSR — admin panel)
  ├── src/layouts/AdminLayout.astro    (sidebar, topbar, nav)
  ├── src/layouts/BaseLayout.astro     (HTML shell, Tailwind)
  ├── src/components/StatsCard.astro   (stat card UI)
  ├── src/components/RepoTable.astro   (repo CRUD table)
  └── src/components/JobStatus.astro   (job badge + auto-poll)
```

---

## Consequences

### Positive
- Single `pnpm run build` produces both the static landing and SSR admin with one adapter.
- Tailwind design system is shared across landing and admin from day one.
- React island upgrade path is pre-wired (`@astrojs/react` installed); M11 dashboard charts drop in as `<Chart client:load />` without restructuring pages.
- `data-testid` convention enforced on all interactive elements enables W7 Playwright E2E without selector fragility.

### Negative / Trade-offs
- Node 20 runtime required on the server (no Python-only deploy).
- Admin SSR pages make a `fetch` to FastAPI on every request; latency is negligible on localhost but must be considered if Astro and FastAPI are split to separate tiers.
- `pnpm` workspace layout means `site/` is a sub-package; `make install` must include `cd site && pnpm install`.

### Deferred
- SaaS billing pages (M9+) will be added as additional Astro pages; no structural changes needed.
- React dashboard charts (M11) will use `client:load` islands; `@astrojs/react` is already present.
- Multi-language i18n (if needed) will use Astro's content collections or a dedicated i18n library.

---

## References

- ADR-0011: Web UI Session Auth (bcrypt cost=12, 8h TTL, SameSite=strict cookie)
- ADR-0015: FastAPI Pure JSON API (W1 routes — the backend this Astro frontend consumes)
- M8 master plan: `docs/superpowers/plans/2026-05-12-milestone-8-astro-unified.md §Stream B W3`
