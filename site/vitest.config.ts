// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // happy-dom gives us window, CustomEvent, fetch-compatible Response, etc.
    // without the full jsdom weight.  All we need is the browser-global surface
    // used by mfaStepUp.ts (window.dispatchEvent / window.addEventListener /
    // CustomEvent / fetch / Response).
    environment: 'happy-dom',

    // Only pick up files under src/**/__tests__/ — keeps Astro pages/components
    // out of the test runner (they need a full Astro + React render pipeline).
    include: ['src/**/__tests__/**/*.test.ts', 'src/**/__tests__/**/*.test.tsx'],

    // Vitest 4 requires explicit glob for TypeScript sources; no transpile quirks
    // because vite handles TS natively.
  },
});
