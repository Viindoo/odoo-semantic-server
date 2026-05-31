import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwind from '@astrojs/tailwind';
import node from '@astrojs/node';
import sitemap from '@astrojs/sitemap';

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
        item.lastmod = '2026-05-31';
        return item;
      },
    }),
  ],
  server: { host: '127.0.0.1', port: 4321 },
  // /api/* is proxied to FastAPI by an SSR endpoint at src/pages/api/[...path].ts
  // (works in pnpm dev AND pnpm preview). Vite's server.proxy was removed because
  // it is dev-only — pnpm preview silently dropped it and admin browser tests 404'd
  // on every client-side fetch('/api/...'). In production nginx handles /api/* before
  // Astro sees it, so the SSR proxy is dead code there but harmless.
});
