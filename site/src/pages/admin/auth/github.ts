// SPDX-License-Identifier: AGPL-3.0-or-later
// site/src/pages/admin/auth/github.ts
// Initiates the GitHub OAuth 2.0 flow (arctic 3.x).
// GitHub does NOT support PKCE — only state (CSRF) cookie is needed.
// F5: generates state, stores in HttpOnly+Secure+SameSite=lax cookie.

import type { APIRoute } from 'astro';
import { GitHub, generateState } from 'arctic';
import { isSafeInternalPath } from '../../../lib/auth-landing';

function _getGitHub(): GitHub {
    return new GitHub(
        import.meta.env.GITHUB_CLIENT_ID,
        import.meta.env.GITHUB_CLIENT_SECRET,
        `${import.meta.env.PUBLIC_BASE_URL}/admin/auth/callback/github`,
    );
}

export const GET: APIRoute = async ({ request, cookies, redirect }) => {
    const state = generateState();

    const github = _getGitHub();
    const url = github.createAuthorizationURL(state, ['read:user', 'user:email']);

    // F5 — store state in HttpOnly cookie (10-min TTL; no verifier for GitHub)
    const cookieOpts = {
        httpOnly: true,
        secure: true,
        sameSite: 'lax' as const,
        maxAge: 600,
        path: '/',
    };
    cookies.set('oauth_state', state, cookieOpts);

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
