// SPDX-License-Identifier: AGPL-3.0-or-later
// /admin/logout: server-side endpoint that POSTs to FastAPI to clear the session
// then redirects to /login.
//
// The AdminLayout sidebar uses <a href="/admin/logout"> (GET). Browsers do not
// auto-issue POST from <a>, so the previous design — only @router.post("/logout")
// in FastAPI — left the session uncleared. This endpoint accepts both GET and POST,
// forwards POST /api/auth/logout to FastAPI server-side, and propagates the
// session-clearing Set-Cookie header back to the browser via 302 redirect.
import type { APIRoute } from 'astro';
import { FASTAPI_BASE } from '../../lib/fastapi';

async function logoutAndRedirect(request: Request): Promise<Response> {
  const headers = new Headers({
    Location: '/login',
    // Clear-Site-Data: defence-in-depth against bfcache attacks on logout.
    // Without this, a user who logs out and presses Back can have the prior
    // session's admin dashboard instantly restored from the browser's bfcache
    // memory snapshot — the server never receives a request so the session
    // guard never runs.
    //
    // "cache" evicts all HTTP cache entries (including bfcache snapshots) for
    // this origin.  "cookies" is belt-and-suspenders on top of the Set-Cookie
    // clear sent from FastAPI.
    //
    // Requires HTTPS (satisfied in production). Supported: Chrome 61+,
    // Firefox 63+, Safari 16.1+.  On non-HTTPS origins the header is silently
    // ignored — acceptable for local dev where bfcache is less of a concern.
    'Clear-Site-Data': '"cache", "cookies"',
  });
  try {
    const upstream = await fetch(`${FASTAPI_BASE}/api/auth/logout`, {
      method: 'POST',
      headers: { cookie: request.headers.get('cookie') ?? '' },
    });
    const setCookie = upstream.headers.get('set-cookie');
    if (setCookie) headers.set('Set-Cookie', setCookie);
  } catch {
    // FastAPI unreachable — still redirect to /login. Without the cookie
    // clear the user's session may linger until the FastAPI TTL elapses, but
    // returning a 5xx here would trap the user on /admin/logout indefinitely.
  }
  return new Response(null, { status: 302, headers });
}

export const GET: APIRoute = ({ request }) => logoutAndRedirect(request);
export const POST: APIRoute = ({ request }) => logoutAndRedirect(request);
