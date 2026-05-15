// site/src/pages/admin/auth/google.ts
// Initiates the Google OAuth 2.0 PKCE flow (arctic 3.x).
// F5: generates state (CSRF) + code_verifier (PKCE), stores both in
// HttpOnly+Secure+SameSite=lax cookies before redirecting to Google.

import type { APIRoute } from 'astro';
import { Google, generateState, generateCodeVerifier } from 'arctic';

function _getGoogle(): Google {
    return new Google(
        import.meta.env.GOOGLE_CLIENT_ID,
        import.meta.env.GOOGLE_CLIENT_SECRET,
        `${import.meta.env.PUBLIC_BASE_URL}/admin/auth/callback/google`,
    );
}

export const GET: APIRoute = async ({ cookies, redirect }) => {
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

    return redirect(url.toString());
};
