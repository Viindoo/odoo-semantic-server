// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Unit tests for auth-landing.ts
 *
 * Environment: happy-dom (via vitest.config.ts) — no browser globals needed
 * for this module; both exports are pure functions operating on strings.
 *
 * We test the two exported functions:
 *   isSafeInternalPath — CWE-601 open-redirect guard; strict allow-list of
 *                        well-formed relative paths.
 *   resolveAuthLanding — Role-aware post-auth landing resolver; priority:
 *                        (1) safe safeReturn → (2) role default.
 */

import { describe, expect, it } from 'vitest';
import { isSafeInternalPath, resolveAuthLanding } from '../auth-landing';

// ---------------------------------------------------------------------------
// isSafeInternalPath
// ---------------------------------------------------------------------------

describe('isSafeInternalPath', () => {
  // ---- accepted paths -------------------------------------------------------

  it('accepts /account/api-keys (typical customer landing)', () => {
    expect(isSafeInternalPath('/account/api-keys')).toBe(true);
  });

  it('accepts /admin/ (admin dashboard with trailing slash)', () => {
    expect(isSafeInternalPath('/admin/')).toBe(true);
  });

  it('accepts /tenant/settings (tenant settings page)', () => {
    expect(isSafeInternalPath('/tenant/settings')).toBe(true);
  });

  it('accepts /account/repos?page=2 (path with query string)', () => {
    expect(isSafeInternalPath('/account/repos?page=2')).toBe(true);
  });

  it('accepts / (root)', () => {
    expect(isSafeInternalPath('/')).toBe(true);
  });

  it('accepts /login (simple path)', () => {
    expect(isSafeInternalPath('/login')).toBe(true);
  });

  // ---- Rule 1: must be non-empty string -------------------------------------

  it('rejects null (non-string)', () => {
    expect(isSafeInternalPath(null)).toBe(false);
  });

  it('rejects undefined (non-string)', () => {
    expect(isSafeInternalPath(undefined)).toBe(false);
  });

  it('rejects empty string', () => {
    expect(isSafeInternalPath('')).toBe(false);
  });

  it('rejects number (non-string)', () => {
    expect(isSafeInternalPath(42)).toBe(false);
  });

  // ---- Rule 2: must start with exactly one '/' -----------------------------

  it('rejects //evil.com (protocol-relative URL)', () => {
    expect(isSafeInternalPath('//evil.com')).toBe(false);
  });

  it('rejects //evil.com/path (protocol-relative with path)', () => {
    expect(isSafeInternalPath('//evil.com/path')).toBe(false);
  });

  it('rejects relative path without leading slash (foo/bar)', () => {
    expect(isSafeInternalPath('foo/bar')).toBe(false);
  });

  it('rejects path starting with a letter (not slash)', () => {
    expect(isSafeInternalPath('evil.com')).toBe(false);
  });

  // ---- Rule 3: must not start with /\ (backslash bypass) -------------------

  it('rejects /\\evil.com (backslash immediately after leading slash)', () => {
    expect(isSafeInternalPath('/\\evil.com')).toBe(false);
  });

  it('rejects /\\path (any /\\ opening)', () => {
    expect(isSafeInternalPath('/\\path')).toBe(false);
  });

  // ---- Rule 4: must not contain :// (sneaked absolute URL) -----------------

  it('rejects https://evil.com (absolute HTTPS URL)', () => {
    expect(isSafeInternalPath('https://evil.com')).toBe(false);
  });

  it('rejects javascript://comment (JS protocol with authority)', () => {
    // Though this starts with 'j' not '/', the :// rule is belt-and-suspenders.
    expect(isSafeInternalPath('javascript://comment')).toBe(false);
  });

  it('rejects /path://smuggled (sneaked :// mid-path)', () => {
    expect(isSafeInternalPath('/path://smuggled')).toBe(false);
  });

  // ---- Rule 5: no backslash anywhere ----------------------------------------

  it('rejects /path\\back (backslash mid-path, host normalisation bypass)', () => {
    expect(isSafeInternalPath('/path\\back')).toBe(false);
  });

  it('rejects /foo\\bar.evil.com (backslash-host bypass)', () => {
    expect(isSafeInternalPath('/foo\\bar.evil.com')).toBe(false);
  });

  // ---- Rule 6: no ASCII control characters ----------------------------------

  it('rejects path containing a null byte (\\x00)', () => {
    expect(isSafeInternalPath('/path\x00evil')).toBe(false);
  });

  it('rejects path containing \\r (CR control char)', () => {
    expect(isSafeInternalPath('/path\r\nevil')).toBe(false);
  });

  it('rejects path containing \\x1f (unit separator control char)', () => {
    expect(isSafeInternalPath('/path\x1fevil')).toBe(false);
  });

  it('rejects path containing \\x7f (DEL control char)', () => {
    expect(isSafeInternalPath('/path\x7fevil')).toBe(false);
  });

  // ---- Special / legacy bypass attempts -------------------------------------

  it('rejects javascript:alert(1) (JS pseudo-protocol — does not start with /)', () => {
    expect(isSafeInternalPath('javascript:alert(1)')).toBe(false);
  });

  it('rejects data:text/html,<svg> (data URI — does not start with /)', () => {
    expect(isSafeInternalPath('data:text/html,<svg>')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// resolveAuthLanding
// ---------------------------------------------------------------------------

describe('resolveAuthLanding', () => {
  // ---- No safeReturn: pure role-based landing --------------------------------

  it('returns /admin/ for admin with no safeReturn', () => {
    expect(resolveAuthLanding(true)).toBe('/admin/');
  });

  it('returns /account/api-keys for non-admin with no safeReturn', () => {
    expect(resolveAuthLanding(false)).toBe('/account/api-keys');
  });

  it('returns /admin/ for admin with null safeReturn', () => {
    expect(resolveAuthLanding(true, null)).toBe('/admin/');
  });

  it('returns /account/api-keys for non-admin with null safeReturn', () => {
    expect(resolveAuthLanding(false, null)).toBe('/account/api-keys');
  });

  it('returns /admin/ for admin with undefined safeReturn', () => {
    expect(resolveAuthLanding(true, undefined)).toBe('/admin/');
  });

  // ---- Valid safeReturn: honoured when safe and role-appropriate -------------

  it('honours /account/api-keys?from=email for non-admin (valid same-role return)', () => {
    expect(resolveAuthLanding(false, '/account/api-keys?from=email')).toBe(
      '/account/api-keys?from=email',
    );
  });

  it('honours /account/repos for non-admin (valid customer path)', () => {
    expect(resolveAuthLanding(false, '/account/repos')).toBe('/account/repos');
  });

  it('honours /admin/repos for admin (valid admin path, same role)', () => {
    expect(resolveAuthLanding(true, '/admin/repos')).toBe('/admin/repos');
  });

  it('honours /admin/ for admin (valid admin root path)', () => {
    expect(resolveAuthLanding(true, '/admin/')).toBe('/admin/');
  });

  it('honours /tenant/settings for non-admin (valid non-admin return)', () => {
    expect(resolveAuthLanding(false, '/tenant/settings')).toBe('/tenant/settings');
  });

  // ---- Unsafe safeReturn: ignored → role default ----------------------------

  it('ignores //evil.com (open-redirect attempt) for admin → /admin/', () => {
    expect(resolveAuthLanding(true, '//evil.com')).toBe('/admin/');
  });

  it('ignores //evil.com for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '//evil.com')).toBe('/account/api-keys');
  });

  it('ignores https://evil.com (absolute URL) for admin → /admin/', () => {
    expect(resolveAuthLanding(true, 'https://evil.com')).toBe('/admin/');
  });

  it('ignores javascript:alert(1) for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, 'javascript:alert(1)')).toBe('/account/api-keys');
  });

  it('ignores /path\\back (backslash bypass) for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '/path\\back')).toBe('/account/api-keys');
  });

  it('ignores empty string for admin → /admin/', () => {
    expect(resolveAuthLanding(true, '')).toBe('/admin/');
  });

  // ---- /admin/* return for non-admin: silently stripped to customer landing -

  it('strips /admin return path (bare, no trailing slash) for non-admin → /account/api-keys', () => {
    // ?return=/admin passes isSafeInternalPath but must be stripped — middleware
    // would redirect /admin → /admin/ causing a second bounce.
    expect(resolveAuthLanding(false, '/admin')).toBe('/account/api-keys');
  });

  it('honours /admin (bare, no trailing slash) for admin → /admin', () => {
    // Admin users may legitimately land on /admin; do not strip it.
    expect(resolveAuthLanding(true, '/admin')).toBe('/admin');
  });

  it('strips /admin/ return path for non-admin → /account/api-keys (not double-bounced)', () => {
    // A crafted ?return=/admin/ must not land non-admins on the admin page.
    // The helper strips it and returns the customer landing immediately (1 redirect).
    expect(resolveAuthLanding(false, '/admin/')).toBe('/account/api-keys');
  });

  it('strips /admin/repos for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '/admin/repos')).toBe('/account/api-keys');
  });

  it('strips /admin/users for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '/admin/users')).toBe('/account/api-keys');
  });

  it('strips /admin/settings for non-admin → /account/api-keys', () => {
    expect(resolveAuthLanding(false, '/admin/settings')).toBe('/account/api-keys');
  });
});
