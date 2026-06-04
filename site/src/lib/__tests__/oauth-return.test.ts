// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// NOTE on environment: this file uses the `node` (undici) environment rather
// than the suite-default `happy-dom`. happy-dom strips the forbidden `Set-Cookie`
// header from both client-constructed and returned `Response` objects, so the
// WI-B cookie-forwarding assertions below (which read Set-Cookie off a Response)
// are unobservable there. undici's WHATWG fetch implementation preserves
// Set-Cookie and exposes `Headers.getSetCookie()`, matching the real Node SSR
// runtime that executes buildOAuthCallbackResponse in production.
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
import { buildOAuthCallbackResponse } from '../fastapi';

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

// ---------------------------------------------------------------------------
// WI-B — auto-minted API key is carried to the landing via an osm_new_key cookie.
//
// buildOAuthCallbackResponse parses the FastAPI body server-side and returns a
// 302; the client JS cannot read the response body, so a brand-new OAuth signup's
// plaintext key must ride to the api-keys page in a short-lived JS-readable cookie.
//
// These tests assert the observable HTTP behaviour: the set of Set-Cookie headers
// on the returned 302. The FastAPI session cookie must always survive (Headers.append,
// not an object literal that would overwrite it).
// ---------------------------------------------------------------------------

const SESSION_COOKIE = 'osm_session=sess-abc; Path=/; HttpOnly; SameSite=Lax';

/**
 * Build a fake successful FastAPI /api/auth/oauth-login Response with a session
 * Set-Cookie header and the given JSON body (is_admin + optional new_api_key).
 */
function makeOAuthApiRes(body: Record<string, unknown>): Response {
  const headers = new Headers({ 'Content-Type': 'application/json' });
  // append (not the constructor literal) so Set-Cookie is preserved reliably.
  headers.append('set-cookie', SESSION_COOKIE);
  return new Response(JSON.stringify(body), { status: 200, headers });
}

/** Read all Set-Cookie header lines from a Response in a runtime-portable way. */
function setCookies(res: Response): string[] {
  const h = res.headers as Headers & { getSetCookie?: () => string[] };
  if (typeof h.getSetCookie === 'function') {
    return h.getSetCookie();
  }
  return [...res.headers].filter(([k]) => k.toLowerCase() === 'set-cookie').map(([, v]) => v);
}

describe('WI-B callback — osm_new_key cookie carries the auto-minted API key', () => {
  it('new_api_key + non-admin landing → 2 Set-Cookie (session + osm_new_key)', async () => {
    const apiRes = makeOAuthApiRes({ is_admin: false, new_api_key: 'osm_test123' });
    const res = await buildOAuthCallbackResponse(apiRes, 'signup', '[test]', '');

    // Lands on the customer api-keys page.
    expect(res.status).toBe(302);
    expect(res.headers.get('Location')).toBe('/account/api-keys');

    const cookies = setCookies(res);
    // Session cookie must survive alongside the new osm_new_key cookie.
    expect(cookies.length).toBe(2);
    expect(cookies.some((c) => c.includes('osm_session=sess-abc'))).toBe(true);

    const newKeyCookie = cookies.find((c) => c.startsWith('osm_new_key='));
    expect(newKeyCookie).toBeDefined();
    expect(newKeyCookie!).toContain('SameSite=Lax');
    expect(newKeyCookie!).not.toContain('SameSite=Strict');
    expect(newKeyCookie!).toContain('Path=/account/api-keys');
    expect(newKeyCookie!).toContain('Max-Age=60');

    // The plaintext key round-trips (URL-decoded) so the page can display it.
    const value = newKeyCookie!.slice('osm_new_key='.length).split(';')[0];
    expect(decodeURIComponent(value)).toBe('osm_test123');
  });

  it('new_api_key null → only the session cookie (no osm_new_key)', async () => {
    const apiRes = makeOAuthApiRes({ is_admin: false, new_api_key: null });
    const res = await buildOAuthCallbackResponse(apiRes, 'signup', '[test]', '');

    const cookies = setCookies(res);
    expect(cookies.length).toBe(1);
    expect(cookies[0]).toContain('osm_session=sess-abc');
    expect(cookies.some((c) => c.startsWith('osm_new_key='))).toBe(false);
  });

  it('new_api_key present but admin landing → no osm_new_key cookie', async () => {
    // is_admin → resolveAuthLanding sends the user to /admin/, not the api-keys page,
    // so the auto-minted key is never surfaced here.
    const apiRes = makeOAuthApiRes({ is_admin: true, new_api_key: 'osm_admin999' });
    const res = await buildOAuthCallbackResponse(apiRes, 'signup', '[test]', '');

    expect(res.headers.get('Location')).toBe('/admin/');
    const cookies = setCookies(res);
    expect(cookies.length).toBe(1);
    expect(cookies[0]).toContain('osm_session=sess-abc');
    expect(cookies.some((c) => c.startsWith('osm_new_key='))).toBe(false);
  });

  it('new_api_key + non-admin with a non-keys ?return= → forced to /account/api-keys so the key is not lost', async () => {
    // A brand-new non-admin signup who deep-linked in with ?return=/account/repos
    // must still see its one-time key: the key is only revealed on /account/api-keys,
    // so we override the deep-link landing rather than silently drop the plaintext.
    const apiRes = makeOAuthApiRes({ is_admin: false, new_api_key: 'osm_deep456' });
    const res = await buildOAuthCallbackResponse(apiRes, 'signup', '[test]', '/account/repos');

    expect(res.headers.get('Location')).toBe('/account/api-keys');
    const cookies = setCookies(res);
    expect(cookies.length).toBe(2);
    const newKeyCookie = cookies.find((c) => c.startsWith('osm_new_key='));
    expect(newKeyCookie).toBeDefined();
    expect(decodeURIComponent(newKeyCookie!.slice('osm_new_key='.length).split(';')[0])).toBe('osm_deep456');
  });

  it('no new_api_key + non-admin with a non-keys ?return= → honours the deep link (no override)', async () => {
    // Without a minted key there is nothing to reveal, so the WS1 deep-link return is preserved.
    const apiRes = makeOAuthApiRes({ is_admin: false, new_api_key: null });
    const res = await buildOAuthCallbackResponse(apiRes, 'login', '[test]', '/account/repos');

    expect(res.headers.get('Location')).toBe('/account/repos');
    const cookies = setCookies(res);
    expect(cookies.length).toBe(1);
    expect(cookies.some((c) => c.startsWith('osm_new_key='))).toBe(false);
  });
});
