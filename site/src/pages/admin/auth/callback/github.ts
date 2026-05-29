// SPDX-License-Identifier: AGPL-3.0-or-later
// site/src/pages/admin/auth/callback/github.ts
// Handles the GitHub OAuth callback: validates state (CSRF),
// exchanges code, fetches user + verified primary email,
// then calls FastAPI /api/auth/oauth-login.
// F5: state CSRF check is mandatory — mismatch returns 403.

import type { APIRoute } from 'astro';
import { GitHub, OAuth2RequestError } from 'arctic';
import { resolveAuthLanding } from '../../../../lib/auth-landing';

function _getGitHub(): GitHub {
    return new GitHub(
        import.meta.env.GITHUB_CLIENT_ID,
        import.meta.env.GITHUB_CLIENT_SECRET,
        `${import.meta.env.PUBLIC_BASE_URL}/admin/auth/callback/github`,
    );
}

interface GitHubUser {
    id: number;
    login: string;
    name: string | null;
}

interface GitHubEmail {
    email: string;
    primary: boolean;
    verified: boolean;
}

export const GET: APIRoute = async ({ request, cookies }) => {
    const url = new URL(request.url);
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');
    const storedState = cookies.get('oauth_state')?.value;

    // F5 — mandatory: validate state (CSRF)
    if (!code || !state || !storedState || state !== storedState) {
        return new Response('Invalid OAuth callback: state mismatch or missing parameters', {
            status: 403,
        });
    }

    // Read oauth_from (set by init if ?from=signup) BEFORE any early return so
    // we can consume it unconditionally — it must be single-use on every exit
    // path (success / 403 / error), otherwise a stale value (600s TTL) could
    // misroute a later, unrelated error.
    const oauthFrom = cookies.get('oauth_from')?.value ?? '';

    // Consume state + from cookies immediately (single-use)
    cookies.delete('oauth_state', { path: '/' });
    cookies.delete('oauth_from', { path: '/' });

    let tokens;
    try {
        const github = _getGitHub();
        tokens = await github.validateAuthorizationCode(code);
    } catch (err) {
        if (err instanceof OAuth2RequestError) {
            return new Response(`OAuth error: ${err.message}`, { status: 400 });
        }
        console.error('[OAuth/github callback] token exchange failed:', err);
        return new Response('Token exchange failed', { status: 502 });
    }

    const accessToken = tokens.accessToken();
    const authHeader = { Authorization: `Bearer ${accessToken}` };

    // Fetch GitHub user profile
    let ghUser: GitHubUser;
    try {
        const res = await fetch('https://api.github.com/user', {
            headers: { ...authHeader, Accept: 'application/vnd.github+json' },
        });
        if (!res.ok) {
            return new Response('Failed to fetch GitHub user', { status: 502 });
        }
        ghUser = (await res.json()) as GitHubUser;
    } catch (err) {
        console.error('[OAuth/github callback] user fetch failed:', err);
        return new Response('Failed to fetch GitHub user', { status: 502 });
    }

    // Fetch verified primary email
    let primaryEmail: string | null = null;
    let emailVerified = false;
    try {
        const res = await fetch('https://api.github.com/user/emails', {
            headers: { ...authHeader, Accept: 'application/vnd.github+json' },
        });
        if (res.ok) {
            const emails = (await res.json()) as GitHubEmail[];
            const primary = emails.find((e) => e.primary && e.verified);
            if (primary) {
                primaryEmail = primary.email;
                emailVerified = true;
            } else {
                // Fall back to any primary (unverified)
                const anyPrimary = emails.find((e) => e.primary);
                if (anyPrimary) {
                    primaryEmail = anyPrimary.email;
                    emailVerified = anyPrimary.verified;
                }
            }
        }
    } catch (err) {
        console.error('[OAuth/github callback] email fetch failed (non-fatal):', err);
    }

    if (!primaryEmail) {
        return new Response(null, {
            status: 302,
            headers: { Location: '/login?error=no_email' },
        });
    }

    // POST to FastAPI to upsert user + issue session cookie
    const apiBase = import.meta.env.API_BASE_URL ?? 'http://localhost:8003';
    let apiRes: Response;
    try {
        apiRes = await fetch(`${apiBase}/api/auth/oauth-login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider: 'github',
                oauth_id: String(ghUser.id),
                email: primaryEmail,
                email_verified: emailVerified,
                name: ghUser.name ?? ghUser.login,
            }),
        });
    } catch (err) {
        console.error('[OAuth/github callback] FastAPI call failed:', err);
        return new Response('Internal error contacting API', { status: 502 });
    }

    if (!apiRes.ok) {
        // Read body once as text; parse JSON separately (stream can only be read once).
        const bodyText = await apiRes.text().catch(() => '');
        console.error('[OAuth/github callback] FastAPI rejected login:', apiRes.status, bodyText);
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
    const body = await apiRes.json().catch(() => ({ is_admin: false })) as { is_admin?: boolean };
    const landing = resolveAuthLanding(body.is_admin === true);

    // Forward session cookie from FastAPI → browser, then redirect to role-aware landing
    const setCookieHeader = apiRes.headers.get('set-cookie');
    return new Response(null, {
        status: 302,
        headers: {
            Location: landing,
            ...(setCookieHeader ? { 'Set-Cookie': setCookieHeader } : {}),
        },
    });
};
