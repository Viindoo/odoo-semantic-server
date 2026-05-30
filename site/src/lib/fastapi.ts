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

    // Parse is_admin from FastAPI success body to determine correct landing page.
    // Guard against parse failure — default to non-admin landing (safest fallback).
    // WS1: forward oauthReturn (already CWE-601-validated at init time) so the
    // user lands on the page they originally tried to visit rather than the
    // role default. resolveAuthLanding applies its own guard as belt-and-suspenders.
    const body = await apiRes.json().catch(() => ({ is_admin: false })) as { is_admin?: boolean };
    const landing = resolveAuthLanding(body.is_admin === true, oauthReturn || undefined);

    // Forward session cookie from FastAPI → browser, then redirect to role-aware landing
    const setCookieHeader = apiRes.headers.get('set-cookie');
    return new Response(null, {
        status: 302,
        headers: {
            Location: landing,
            ...(setCookieHeader ? { 'Set-Cookie': setCookieHeader } : {}),
        },
    });
}
