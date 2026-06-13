import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwind from '@astrojs/tailwind';
import node from '@astrojs/node';
import sitemap from '@astrojs/sitemap';
import { parseDevOrigin } from './src/lib/check-origin-config.mjs';

// --- checkOrigin / allowedDomains explanation (Astro 6.3.3, issue #236) ---
//
// Astro's `security.checkOrigin` guards mutating requests (POST/PATCH/DELETE)
// by comparing the request's Origin header against `url.origin`.  `url.origin`
// is derived from the HTTP Host header — BUT only when `security.allowedDomains`
// contains an entry that matches that host.  With an *empty* allowedDomains list,
// `validateHost()` returns undefined and `hostname` falls back to "localhost",
// so `url.origin` becomes "http://localhost:4321" regardless of what Host was sent.
//
// In dev the server binds to 127.0.0.1:4321, so the browser opens the page at
// http://127.0.0.1:4321 and every fetch() sends
//   Origin: http://127.0.0.1:4321
// That never equals "http://localhost:4321", so `isSameOrigin = false`.
// Multipart uploads (Content-Type: multipart/form-data — the restore endpoint)
// are "form-like" in Astro's heuristic, so checkOrigin returns 403 even though
// the request is genuinely same-origin.  JSON requests skip the formLikeHeader
// branch, which is why they pass unaffected.
//
// Fix: tell Astro that 127.0.0.1:4321 is a trusted host so validateHost returns
// the real hostname instead of falling back to "localhost".  This is gated on the
// ASTRO_DEV_ORIGIN env var.  checkOrigin stays ON everywhere; we only widen the
// trusted-host set when needed so that the origin comparison is accurate.
//
// TIMING — this config is evaluated at BUILD TIME (astro build) or at dev-server
// startup (astro dev).  It is NOT re-evaluated when `astro preview` serves the
// already-built output.  Therefore:
//
//   pnpm dev                   → sets ASTRO_DEV_ORIGIN in the script, config
//                                re-evaluated on restart → allowedDomains populated.
//   pnpm build                 → env unset → allowedDomains empty (prod default).
//   pnpm build:dev             → sets ASTRO_DEV_ORIGIN at build time → allowedDomains
//                                baked into the output.
//   pnpm build:dev && pnpm preview → serves the build:dev output → allowedDomains
//                                    present → 127.0.0.1 origin accepted.
//   pnpm build && pnpm preview → allowedDomains empty → 127.0.0.1 still 403.
//   pnpm preview:dev           → alias for build:dev + preview (convenience).
//
// Security posture (prod): nginx routes /api/* to FastAPI before Astro sees the
// request, so this SSR proxy handler never runs.  Even if it did, allowedDomains
// would be empty (ASTRO_DEV_ORIGIN unset in prod) and the fallback "localhost" would
// correctly mismatch any external origin.
//
// parseDevOrigin is the SSOT for the URL-parsing logic — shared with the test suite
// (site/src/lib/check-origin-config.mjs).
const devAllowedDomains = parseDevOrigin(process.env.ASTRO_DEV_ORIGIN);

export default defineConfig({
  // Canonical public origin — powers Astro.site for absolute OG/Twitter image
  // URLs. Override per-deploy when self-hosting under a different domain.
  site: 'https://odoo-semantic.viindoo.com',
  // output: 'server' enables SSR with per-page opt-out via export const prerender = true
  // ('hybrid' was merged into 'server' in Astro 5.x)
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  integrations: [
    react(),
    tailwind(),
    sitemap({
      // Exclude auth, account, admin, and tenant pages from the public sitemap.
      filter: (page) => ![
        '/login',
        '/signup',
        '/forgot-password',
        '/reset-password',
        '/verify-email',
      ].some((path) => page.endsWith(path) || page.endsWith(path + '/'))
        && !page.includes('/admin/')
        && !page.includes('/account/')
        && !page.includes('/tenant/'),
      // /pricing is prerender=false (SSR) so @astrojs/sitemap cannot discover it
      // automatically — include it explicitly so search engines index the page.
      customPages: ['https://odoo-semantic.viindoo.com/pricing/'],
      // Add a stable lastmod to every sitemap entry so crawlers can detect
      // content freshness. Using a fixed build-time constant avoids non-determinism
      // from new Date() at config-parse time.
      serialize(item) {
        item.lastmod = '2026-06-13';
        return item;
      },
    }),
  ],
  server: { host: '127.0.0.1', port: 4321 },
  // checkOrigin stays true (default) in all environments.
  // allowedDomains is non-empty only when ASTRO_DEV_ORIGIN was set at BUILD TIME
  // (pnpm dev or pnpm build:dev) so the origin comparison uses the real Host header
  // rather than the "localhost" fallback.  See the long comment at the top of this file.
  security: {
    checkOrigin: true,
    ...(devAllowedDomains.length > 0 ? { allowedDomains: devAllowedDomains } : {}),
  },
  // /api/* is proxied to FastAPI by an SSR endpoint at src/pages/api/[...path].ts
  // (works in pnpm dev AND pnpm preview). Vite's server.proxy was removed because
  // it is dev-only — pnpm preview silently dropped it and admin browser tests 404'd
  // on every client-side fetch('/api/...'). In production nginx handles /api/* before
  // Astro sees it, so the SSR proxy is dead code there but harmless.
});
