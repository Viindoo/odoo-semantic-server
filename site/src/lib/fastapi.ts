// SPDX-License-Identifier: AGPL-3.0-or-later
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

// ---------------------------------------------------------------------------
// Shared OAuth callback response builder (WI-DRY)
// ---------------------------------------------------------------------------

import { resolveAuthLanding } from './auth-landing';

/**
 * Shared post-FastAPI response builder for OAuth callbacks (google + github).
 *
 * Both callback handlers call POST /api/auth/oauth-login and share identical
 * error-handling + success-redirect logic.  Extracting it here eliminates the
 * drift risk from maintaining two CHARACTER-IDENTICAL ~45-line blocks.
 *
 * Behaviour (preserved byte-for-byte from the original callback files):
 *   - 409   → redirect /login?error=email_conflict
 *   - 403 signup_disabled → redirect to oauthFrom-origin page + ?error=signup_disabled
 *   - other error → redirect /login?error=oauth_failed
 *   - success → parse is_admin, resolveAuthLanding(isAdmin, oauthReturn),
 *               forward Set-Cookie, redirect
 *
 * @param apiRes      - The Response from FastAPI /api/auth/oauth-login (already
 *                      awaited; body NOT yet consumed by the caller).
 * @param oauthFrom   - Value of the `oauth_from` cookie ('signup' | '').
 * @param logPrefix   - Provider-specific log prefix, e.g. '[OAuth/google callback]'.
 * @param oauthReturn - Value of the `oauth_return` cookie (validated safe path | '').
 *                      Forwarded to resolveAuthLanding so the user returns to the
 *                      page they originally tried to visit (WS1).
 */
export async function buildOAuthCallbackResponse(
    apiRes: Response,
    oauthFrom: string,
    logPrefix: string,
    oauthReturn: string = '',
): Promise<Response> {
    if (!apiRes.ok) {
        // Read body once as text; parse JSON separately (stream can only be read once).
        const bodyText = await apiRes.text().catch(() => '');
        console.error(`${logPrefix} FastAPI rejected login:`, apiRes.status, bodyText);
        // 409 = email collision with unverified account
        if (apiRes.status === 409) {
            return new Response(null, {
                status: 302,
                headers: { Location: '/login?error=email_conflict' },
            });
        }
        // 403 signup_disabled — redirect to origin page with specific error.
        // O-A: use the oauth_from value read+cleared at the top (single-use) to
        // pick the correct error destination.
        if (apiRes.status === 403) {
            const errBody = (() => { try { return JSON.parse(bodyText) as { error?: string }; } catch { return {}; } })();
            if (errBody.error === 'signup_disabled') {
                const dest = oauthFrom === 'signup' ? '/signup' : '/login';
                return new Response(null, {
                    status: 302,
                    headers: { Location: `${dest}?error=signup_disabled` },
                });
            }
        }
        return new Response(null, {
            status: 302,
            headers: { Location: '/login?error=oauth_failed' },
        });
    }

    // Parse is_admin + new_api_key from FastAPI success body.
    //   - is_admin determines the correct landing page.
    //   - new_api_key (plaintext osm_... | null) is the auto-minted key handed
    //     to brand-new OAuth signups; it is carried to the landing page via a
    //     short-lived, JS-readable cookie (see below).
    // Guard against parse failure — default to non-admin landing (safest fallback).
    // WS1: forward oauthReturn (already CWE-601-validated at init time) so the
    // user lands on the page they originally tried to visit rather than the
    // role default. resolveAuthLanding applies its own guard as belt-and-suspenders.
    const body = await apiRes.json().catch(() => ({ is_admin: false })) as {
        is_admin?: boolean;
        new_api_key?: string | null;
    };
    const isAdmin = body.is_admin === true;
    // A brand-new non-admin OAuth signup gets exactly one chance to see its
    // auto-minted key, and it is only revealed on /account/api-keys. If we honoured
    // a deep-link oauthReturn (e.g. /account/repos) here, the key cookie would not be
    // set for that landing and the plaintext would be lost forever (lazy-mint is
    // idempotent and GET never re-exposes it). Seeing the only key trumps the deep
    // link for a just-created account — force the api-keys landing in that case.
    // Admins keep their normal landing (they don't get a key surfaced here).
    const landing = (body.new_api_key && !isAdmin)
        ? '/account/api-keys'
        : resolveAuthLanding(isAdmin, oauthReturn || undefined);

    // Forward session cookie from FastAPI → browser, then redirect to role-aware landing.
    //
    // Use Headers (not an object literal) so we can emit MULTIPLE Set-Cookie headers.
    // An object literal can only hold one 'Set-Cookie' key — a second assignment would
    // overwrite the first, silently dropping the FastAPI session cookie and logging the
    // user out. Headers.append preserves every Set-Cookie line.
    const headers = new Headers({ Location: landing });

    const setCookieHeader = apiRes.headers.get('set-cookie');
    if (setCookieHeader) {
        headers.append('Set-Cookie', setCookieHeader);
    }

    // Carry the auto-minted API key to the landing page so it can be shown once.
    // Only when the key is present AND the user is heading to the api-keys page
    // (admins land elsewhere and never get an auto-minted key surfaced here).
    //
    // SameSite=Lax — NEVER Strict. The OAuth callback redirects via a cross-site
    // top-level GET hop (provider → /admin/auth/callback → landing); browsers drop
    // SameSite=Strict cookies on that hop, so this cookie must be Lax to survive to
    // the landing GET (mirrors the session-cookie reasoning in src/web_ui/app.py).
    //
    // NOT HttpOnly — the landing page's client JS must read this value to display
    // the key. Short Max-Age (60s) + narrow Path limit the exposure window.
    //
    // Secure flag derives from WEBUI_SECURE_COOKIE (the same SSOT the backend
    // session cookie uses — app.py:166); Secure by default, opt-out only for
    // local plain-HTTP dev via WEBUI_SECURE_COOKIE=0.
    if (body.new_api_key && landing === '/account/api-keys') {
        const secure = env?.WEBUI_SECURE_COOKIE !== '0';
        let cookie =
            `osm_new_key=${encodeURIComponent(body.new_api_key)}` +
            '; Path=/account/api-keys; Max-Age=60; SameSite=Lax';
        if (secure) {
            cookie += '; Secure';
        }
        headers.append('Set-Cookie', cookie);
    }

    return new Response(null, { status: 302, headers });
}
