// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * auth-landing — Centralised post-authentication redirect helper (WI-3).
 *
 * ## Problem
 * Multiple auth completion points (password login, OAuth callbacks, email
 * verification, signup already-authed branch) previously hardcoded `/admin/`
 * as the success landing.  Non-admin customers were bounced by middleware to
 * `/account/api-keys` — two redirects instead of one, and a confusing `/admin`
 * flash.  This module provides a single source of truth.
 *
 * ## Usage
 *
 *   import { resolveAuthLanding } from '../lib/auth-landing';
 *
 *   // After a login response that includes is_admin:
 *   window.location.href = resolveAuthLanding(data.is_admin, rawReturn);
 *
 *   // In SSR (Astro) — already-authed branch:
 *   return Astro.redirect(resolveAuthLanding(verifyData.is_admin));
 *
 * ## Security — Open-redirect (CWE-601)
 * The optional `safeReturn` parameter is caller-supplied and may be attacker-
 * controlled (e.g. `?return=` query param).  It is only honoured when it passes
 * `isSafeInternalPath()`, which enforces the same rules as the CWE-601 guard in
 * `site/src/pages/login.astro` lines 25-30 and 225-230, extended with additional
 * checks against protocol-relative (`//`) and backslash-normalisation (`/\`)
 * bypass techniques.
 *
 * The guard also strips `/admin/*` return paths for non-admin users so a
 * carefully crafted `?return=/admin/secret` cannot be used to probe admin
 * routes — middleware would bounce them anyway, but one redirect is better
 * than two.
 */

// ---------------------------------------------------------------------------
// Open-redirect guard (CWE-601)
// ---------------------------------------------------------------------------

/**
 * Return `true` iff `p` is safe to use as a same-origin redirect target.
 *
 * Rules (mirrors login.astro CWE-601 guard, extended):
 *   1. Must be a non-empty string.
 *   2. Must start with exactly one `/` — rejects `//foo` (protocol-relative)
 *      and empty/relative paths.
 *   3. Must NOT start with `/\` — rejects `\/foo` backslash bypass (some
 *      parsers normalise `\/` to `//` or treat it as a network path).
 *   4. Must NOT contain `://` — rejects sneaked absolute URLs like `/foo://`.
 *   5. Must NOT contain a backslash anywhere — rejects paths that browsers
 *      may normalise to a host component (`/foo\bar.evil.com`).
 *   6. Must NOT contain ASCII control characters (U+0000–U+001F, U+007F) —
 *      rejects header-injection / bypass via encoded control chars.
 *
 * This is intentionally strict: only well-formed relative paths survive.
 * Callers do NOT need to pre-sanitise `p` before passing it here.
 *
 * @param p - Candidate redirect path (attacker-controlled).
 */
export function isSafeInternalPath(p: unknown): p is string {
  if (typeof p !== 'string' || p.length === 0) return false;

  // Rule 2 — must start with exactly one '/'
  if (!p.startsWith('/')) return false;
  if (p.startsWith('//')) return false;

  // Rule 3 — backslash immediately after leading slash
  if (p.startsWith('/\\')) return false;

  // Rule 4 — sneaked absolute URL containing '://'
  if (p.includes('://')) return false;

  // Rule 5 — any backslash (host-normalisation bypass)
  if (p.includes('\\')) return false;

  // Rule 6 — ASCII control characters
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(p)) return false;

  return true;
}

// ---------------------------------------------------------------------------
// Default landings per role
// ---------------------------------------------------------------------------

/** Landing URL for admin users when no explicit return target is provided. */
const ADMIN_LANDING = '/admin/';

/** Landing URL for non-admin (customer) users. */
const CUSTOMER_LANDING = '/account/api-keys';

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

/**
 * Resolve the correct post-authentication landing URL in a single hop.
 *
 * Priority order:
 *   1. If `safeReturn` is provided and passes `isSafeInternalPath()`:
 *      - If the user is NOT an admin and the path starts with `/admin/`,
 *        silently fall through to the role default (avoids a middleware
 *        bounce and prevents probing admin routes).
 *      - Otherwise return `safeReturn` as-is.
 *   2. Admin → `/admin/`
 *   3. Non-admin (customer) → `/account/api-keys`
 *
 * The `safeReturn` parameter is typically the raw `?return=` query-string
 * value forwarded from middleware (set when an unauthenticated user requests
 * an `/account/*` path).  It is safe to pass attacker-controlled input here
 * because `isSafeInternalPath()` enforces strict CWE-601 guards before the
 * value is used.
 *
 * @param isAdmin   - `true` if the authenticated user has `is_admin = true`
 *                    in the database (from `/api/auth/verify` `is_admin` field
 *                    or from the login/oauth-login response).
 * @param safeReturn - Raw candidate return path, typically from `?return=`
 *                    (may be attacker-controlled; validated internally).
 * @returns Absolute-path URL string safe to use in `Location` header or
 *          `window.location.href`.
 */
export function resolveAuthLanding(isAdmin: boolean, safeReturn?: string | null): string {
  if (safeReturn != null && isSafeInternalPath(safeReturn)) {
    // Strip /admin/* return paths for non-admin users — prevents a second
    // middleware bounce and closes a probing vector for admin routes.
    if (!isAdmin && safeReturn.startsWith('/admin/')) {
      return CUSTOMER_LANDING;
    }
    return safeReturn;
  }

  return isAdmin ? ADMIN_LANDING : CUSTOMER_LANDING;
}
