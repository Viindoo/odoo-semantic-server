import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwind from '@astrojs/tailwind';
import node from '@astrojs/node';

export default defineConfig({
  // output: 'server' enables SSR with per-page opt-out via export const prerender = true
  // ('hybrid' was merged into 'server' in Astro 5.x)
  output: 'server',
  adapter: node({ mode: 'standalone' }),
  integrations: [react(), tailwind()],
  server: { host: '127.0.0.1', port: 4321 },
  // /api/* is proxied to FastAPI by an SSR endpoint at src/pages/api/[...path].ts
  // (works in pnpm dev AND pnpm preview). Vite's server.proxy was removed because
  // it is dev-only — pnpm preview silently dropped it and admin browser tests 404'd
  // on every client-side fetch('/api/...'). In production nginx handles /api/* before
  // Astro sees it, so the SSR proxy is dead code there but harmless.
});
