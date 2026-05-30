// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the OAuth ?return= round-trip (WS1).
 *
 * What these tests cover:
 *   1. Safe ?return= paths survive `isSafeInternalPath` validation and are
 *      honoured by `resolveAuthLanding` (the final step in the round-trip).
 *   2. Unsafe values (open-redirect attempts) are rejected by
 *      `isSafeInternalPath` at the init-endpoint layer and, even if a stale
 *      cookie somehow contained them, resolveAuthLanding falls back to the
 *      role default.
 *   3. Non-admin users cannot be sent to /admin/* via ?return= — the helper
 *      silently strips those paths.
 *   4. The `oauthReturn` empty-string default (absent cookie) causes
 *      resolveAuthLanding to apply the pure role-default logic.
 *
 * Cookie-layer logic lives in server-side Astro route handlers and cannot be
 * exercised in a Vitest unit context (no real HTTP, no arctic, no cookie jar).
 * These tests focus on the two pure-function layers that are testable:
 *   - isSafeInternalPath  (init-endpoint guard — gates what gets stored)
 *   - resolveAuthLanding  (callback success — determines the final Location)
 *
 * Style mirrors site/src/lib/__tests__/auth-landing.test.ts.
 */

import { describe, expect, it } from 'vitest';
import { isSafeInternalPath, resolveAuthLanding } from '../auth-landing';

// ---------------------------------------------------------------------------
// Init-endpoint guard: isSafeInternalPath decides what gets stored in the
// oauth_return cookie.  If it returns false the cookie is not set.
// ---------------------------------------------------------------------------

describe('WS1 init — isSafeInternalPath gates oauth_return cookie storage', () => {
  // ---- Values that SHOULD be stored (safe → cookie set) --------------------

  it('accepts /account/api-keys — customer landing after OAuth', () => {
    expect(isSafeInternalPath('/account/api-keys')).toBe(true);
  });

  it('accepts /account/repos — customer repos page', () => {
    expect(isSafeInternalPath('/account/repos')).toBe(true);
  });

  it('accepts /account/usage — customer usage page', () => {
    expect(isSafeInternalPath('/account/usage')).toBe(true);
  });

  it('accepts /admin/repos — admin page (admin user ?return= flow)', () => {
    expect(isSafeInternalPath('/admin/repos')).toBe(true);
  });

  it('accepts /tenant/settings — tenant settings page', () => {
    expect(isSafeInternalPath('/tenant/settings')).toBe(true);
  });

  it('accepts a path with query string (/account/repos?page=2)', () => {
    expect(isSafeInternalPath('/account/repos?page=2')).toBe(true);
  });

  // ---- Values that MUST NOT be stored (unsafe → cookie not set) ------------

  it('rejects //evil.com (protocol-relative open-redirect)', () => {
    expect(isSafeInternalPath('//evil.com')).toBe(false);
  });

  it('rejects https://evil.com (absolute URL)', () => {
    expect(isSafeInternalPath('https://evil.com')).toBe(false);
  });

  it('rejects /\\evil.com (backslash bypass)', () => {
    expect(isSafeInternalPath('/\\evil.com')).toBe(false);
  });

  it('rejects javascript:alert(1) (JS pseudo-protocol)', () => {
    expect(isSafeInternalPath('javascript:alert(1)')).toBe(false);
  });

  it('rejects empty string (absent ?return= param)', () => {
    // In practice an absent param → returnPath is null → not passed to
    // isSafeInternalPath at all; this covers the defensive branch.
    expect(isSafeInternalPath('')).toBe(false);
  });

  it('rejects null (absent cookie value)', () => {
    expect(isSafeInternalPath(null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Callback success: resolveAuthLanding honours the oauthReturn value when
// safe, or falls back to role default when unsafe/absent.
// ---------------------------------------------------------------------------

describe('WS1 callback — resolveAuthLanding uses oauthReturn from cookie', () => {
  // ---- Safe return survives → user lands where they intended ---------------

  it('non-admin: /account/api-keys return → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '/account/api-keys')).toBe('/account/api-keys');
  });

  it('non-admin: /account/repos return → /account/repos', () => {
    expect(resolveAuthLanding(false, '/account/repos')).toBe('/account/repos');
  });

  it('admin: /admin/repos return → /admin/repos', () => {
    expect(resolveAuthLanding(true, '/admin/repos')).toBe('/admin/repos');
  });

  it('admin: /admin/ return → /admin/', () => {
    expect(resolveAuthLanding(true, '/admin/')).toBe('/admin/');
  });

  it('non-admin: /tenant/settings return → /tenant/settings', () => {
    expect(resolveAuthLanding(false, '/tenant/settings')).toBe('/tenant/settings');
  });

  // ---- Absent / empty cookie → pure role default ---------------------------

  it('non-admin with empty oauthReturn → /account/api-keys (role default)', () => {
    // Simulates absent cookie: callback passes '' → converted to undefined
    // via (oauthReturn || undefined) in buildOAuthCallbackResponse.
    expect(resolveAuthLanding(false, undefined)).toBe('/account/api-keys');
  });

  it('admin with empty oauthReturn → /admin/ (role default)', () => {
    expect(resolveAuthLanding(true, undefined)).toBe('/admin/');
  });

  it('non-admin with null oauthReturn → /account/api-keys (role default)', () => {
    expect(resolveAuthLanding(false, null)).toBe('/account/api-keys');
  });

  // ---- Unsafe values (should not reach here if init guard worked, but
  //      resolveAuthLanding applies its own belt-and-suspenders guard) -------

  it('non-admin: //evil.com open-redirect → /account/api-keys (stripped)', () => {
    expect(resolveAuthLanding(false, '//evil.com')).toBe('/account/api-keys');
  });

  it('admin: //evil.com open-redirect → /admin/ (stripped)', () => {
    expect(resolveAuthLanding(true, '//evil.com')).toBe('/admin/');
  });

  it('non-admin: https://evil.com absolute URL → /account/api-keys (stripped)', () => {
    expect(resolveAuthLanding(false, 'https://evil.com')).toBe('/account/api-keys');
  });

  // ---- /admin/* return for non-admin: silently stripped to customer landing -

  it('non-admin: /admin/ return → /account/api-keys (admin route stripped, 1 hop)', () => {
    // Middleware would have bounced /admin/ → /admin/ loop for non-admin.
    // resolveAuthLanding strips it so there is exactly one redirect.
    expect(resolveAuthLanding(false, '/admin/')).toBe('/account/api-keys');
  });

  it('non-admin: /admin/users return → /account/api-keys (admin route stripped)', () => {
    expect(resolveAuthLanding(false, '/admin/users')).toBe('/account/api-keys');
  });

  it('non-admin: /admin/settings return → /account/api-keys (admin route stripped)', () => {
    expect(resolveAuthLanding(false, '/admin/settings')).toBe('/account/api-keys');
  });
});
