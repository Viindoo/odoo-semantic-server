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
  vite: {
    server: {
      proxy: {
        '/api': 'http://localhost:8003',
      },
    },
  },
});
