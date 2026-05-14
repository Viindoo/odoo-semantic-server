/**
 * Centralised FastAPI base URL for server-side Astro fetches.
 *
 * Reason: middleware.ts and logout.ts honored FASTAPI_BASE env;
 * 6 admin pages hardcoded localhost:8003. Now all SSR fetches read
 * the same env so split-tier deploys work without page-by-page edits.
 *
 * Usage:
 *   import { FASTAPI_BASE } from '../lib/fastapi';
 *   const res = await fetch(`${FASTAPI_BASE}/api/...`, { headers: { cookie } });
 */
const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;
export const FASTAPI_BASE = env?.FASTAPI_BASE || 'http://127.0.0.1:8003';
