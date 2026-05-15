// site/src/pages/admin/auth/callback/google.ts
// Handles the Google OAuth 2.0 callback: validates state + PKCE,
// exchanges code, fetches user info, then calls FastAPI /api/auth/oauth-login.
// F5: state CSRF check + PKCE verifier are mandatory — any mismatch returns 403.

import type { APIRoute } from 'astro';
import { Google, OAuth2RequestError } from 'arctic';

function _getGoogle(): Google {
    return new Google(
        import.meta.env.GOOGLE_CLIENT_ID,
        import.meta.env.GOOGLE_CLIENT_SECRET,
        `${import.meta.env.PUBLIC_BASE_URL}/admin/auth/callback/google`,
    );
}

interface GoogleUserInfo {
    sub: string;
    email: string;
    email_verified: boolean;
    name: string;
}

export const GET: APIRoute = async ({ request, cookies }) => {
    const url = new URL(request.url);
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');
    const storedState = cookies.get('oauth_state')?.value;
    const verifier = cookies.get('oauth_verifier')?.value;

    // F5 — mandatory: validate state (CSRF) and verifier (PKCE)
    if (!code || !state || !storedState || !verifier || state !== storedState) {
        return new Response('Invalid OAuth callback: state mismatch or missing parameters', {
            status: 403,
        });
    }

    // Consume state + verifier cookies immediately (single-use)
    cookies.delete('oauth_state', { path: '/' });
    cookies.delete('oauth_verifier', { path: '/' });

    let tokens;
    try {
        const google = _getGoogle();
        tokens = await google.validateAuthorizationCode(code, verifier);
    } catch (err) {
        if (err instanceof OAuth2RequestError) {
            return new Response(`OAuth error: ${err.message}`, { status: 400 });
        }
        console.error('[OAuth/google callback] token exchange failed:', err);
        return new Response('Token exchange failed', { status: 502 });
    }

    // Fetch user info from Google OIDC endpoint
    let userInfo: GoogleUserInfo;
    try {
        const res = await fetch('https://openidconnect.googleapis.com/v1/userinfo', {
            headers: { Authorization: `Bearer ${tokens.accessToken()}` },
        });
        if (!res.ok) {
            return new Response('Failed to fetch Google user info', { status: 502 });
        }
        userInfo = (await res.json()) as GoogleUserInfo;
    } catch (err) {
        console.error('[OAuth/google callback] userinfo fetch failed:', err);
        return new Response('Failed to fetch user info', { status: 502 });
    }

    if (!userInfo.sub || !userInfo.email) {
        return new Response('Incomplete user info from Google', { status: 502 });
    }

    // POST to FastAPI to upsert user + issue session cookie
    const apiBase = import.meta.env.API_BASE_URL ?? 'http://localhost:8003';
    let apiRes: Response;
    try {
        apiRes = await fetch(`${apiBase}/api/auth/oauth-login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider: 'google',
                oauth_id: userInfo.sub,
                email: userInfo.email,
                email_verified: userInfo.email_verified ?? false,
                name: userInfo.name ?? '',
            }),
        });
    } catch (err) {
        console.error('[OAuth/google callback] FastAPI call failed:', err);
        return new Response('Internal error contacting API', { status: 502 });
    }

    if (!apiRes.ok) {
        const body = await apiRes.text().catch(() => '');
        console.error('[OAuth/google callback] FastAPI rejected login:', apiRes.status, body);
        // 409 = email collision with unverified account
        if (apiRes.status === 409) {
            return new Response(null, {
                status: 302,
                headers: { Location: '/admin/login?error=email_conflict' },
            });
        }
        return new Response(null, {
            status: 302,
            headers: { Location: '/admin/login?error=oauth_failed' },
        });
    }

    // Forward session cookie from FastAPI → browser, then redirect to admin
    const setCookieHeader = apiRes.headers.get('set-cookie');
    return new Response(null, {
        status: 302,
        headers: {
            Location: '/admin/',
            ...(setCookieHeader ? { 'Set-Cookie': setCookieHeader } : {}),
        },
    });
};
