// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Shared ASTRO_DEV_ORIGIN parsing logic — single source of truth.
 *
 * Imported by:
 *   - astro.config.mjs  (populates security.allowedDomains at BUILD TIME)
 *   - src/lib/__tests__/check-origin-config.test.ts  (unit tests)
 *
 * Why a separate module?
 *   astro.config.mjs cannot be imported by vitest directly (Astro wraps the file
 *   and re-runs it at build time).  Extracting the pure logic here keeps the test
 *   exercising the real code rather than a hand-copied duplicate.
 *
 * Timing note (important):
 *   `astro.config.mjs` is evaluated at BUILD TIME (astro build / astro dev startup).
 *   `pnpm dev` re-evaluates config on restart, so ASTRO_DEV_ORIGIN set in the dev
 *   script takes effect immediately.
 *   `pnpm preview` serves a *pre-built* output — the config is NOT re-evaluated at
 *   serve time, so setting ASTRO_DEV_ORIGIN in a preview script would have NO effect.
 *   To get a non-empty allowedDomains in preview you must build with the env var set
 *   (use `pnpm build:dev` instead of `pnpm build`) and then run `pnpm preview`.
 */

/**
 * Parse an origin URL string (e.g. "http://127.0.0.1:4321") into the shape
 * Astro's `security.allowedDomains` expects.
 *
 * Returns an empty array when `env` is undefined, empty, or not a valid URL.
 *
 * @param {string | undefined} env  Value of ASTRO_DEV_ORIGIN
 * @returns {Array<{hostname: string, port: string | undefined, protocol: string}>}
 */
export function parseDevOrigin(env) {
  if (!env) return [];
  try {
    const u = new URL(env);
    return [{ hostname: u.hostname, port: u.port || undefined, protocol: u.protocol.replace(':', '') }];
  } catch {
    return [];
  }
}

/**
 * Replicates the Astro 6.3.3 `checkOrigin` heuristic from
 * `node_modules/astro/dist/core/app/middlewares.js`.
 *
 * Used as EXECUTABLE DOCUMENTATION in the test suite to document expected
 * behaviour and guard against upstream changes that would reintroduce #236.
 *
 * @param {string} method
 * @param {string | null} contentType
 * @param {string | null} originHeader
 * @param {string} urlOrigin  — Astro-computed url.origin (affected by allowedDomains)
 * @returns {boolean}  true = request would be blocked with 403
 */
export function wouldCheckOriginBlock(method, contentType, originHeader, urlOrigin) {
  const FORM_CONTENT_TYPES = [
    'application/x-www-form-urlencoded',
    'multipart/form-data',
    'text/plain',
  ];
  const safeMethods = ['GET', 'HEAD', 'OPTIONS'];
  if (safeMethods.includes(method)) return false;
  const isSameOrigin = originHeader === urlOrigin;
  if (contentType !== null) {
    const formLike = FORM_CONTENT_TYPES.some((t) => contentType.toLowerCase().includes(t));
    return formLike && !isSameOrigin;
  }
  // no content-type
  return !isSameOrigin;
}
