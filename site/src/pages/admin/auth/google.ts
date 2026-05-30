// SPDX-License-Identifier: AGPL-3.0-or-later
// site/src/pages/admin/auth/google.ts
// Initiates the Google OAuth 2.0 PKCE flow (arctic 3.x).
// F5: generates state (CSRF) + code_verifier (PKCE), stores both in
// HttpOnly+Secure+SameSite=lax cookies before redirecting to Google.

import type { APIRoute } from 'astro';
import { Google, generateState, generateCodeVerifier } from 'arctic';
import { isSafeInternalPath } from '../../../lib/auth-landing';

function _getGoogle(): Google {
    return new Google(
        import.meta.env.GOOGLE_CLIENT_ID,
        import.meta.env.GOOGLE_CLIENT_SECRET,
        `${import.meta.env.PUBLIC_BASE_URL}/admin/auth/callback/google`,
    );
}

export const GET: APIRoute = async ({ request, cookies, redirect }) => {
    const state = generateState();
    const verifier = generateCodeVerifier();

    const google = _getGoogle();
    const url = google.createAuthorizationURL(state, verifier, [
        'openid',
        'email',
        'profile',
    ]);

    // F5 — store state + verifier in HttpOnly cookies (10-min TTL)
    const cookieOpts = {
        httpOnly: true,
        secure: true,
        sameSite: 'lax' as const,
        maxAge: 600,
        path: '/',
    };
    cookies.set('oauth_state', state, cookieOpts);
    cookies.set('oauth_verifier', verifier, cookieOpts);

    const params = new URL(request.url).searchParams;

    // O-A origin tracking (ADR auth-unify WI-2): if ?from=signup, record
    // origin so the callback can redirect errors back to the correct page.
    const from = params.get('from') ?? '';
    if (from === 'signup') {
        cookies.set('oauth_from', 'signup', cookieOpts);
    }

    // WS1 — thread ?return= through OAuth flow (CWE-601 guarded):
    // Only store the return path when it passes isSafeInternalPath so the
    // callback can restore the user's original destination after success.
    // Unsafe/absent values are silently dropped — the cookie is never set.
    const returnPath = params.get('return');
    if (returnPath && isSafeInternalPath(returnPath)) {
        cookies.set('oauth_return', returnPath, cookieOpts);
    }

    return redirect(url.toString());
};
