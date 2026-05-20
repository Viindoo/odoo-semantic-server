// SPDX-License-Identifier: AGPL-3.0-or-later
// /admin/logout: server-side endpoint that POSTs to FastAPI to clear the session
// then redirects to /admin/login.
//
// The AdminLayout sidebar uses <a href="/admin/logout"> (GET). Browsers do not
// auto-issue POST from <a>, so the previous design — only @router.post("/logout")
// in FastAPI — left the session uncleared. This endpoint accepts both GET and POST,
// forwards POST /api/auth/logout to FastAPI server-side, and propagates the
// session-clearing Set-Cookie header back to the browser via 302 redirect.
import type { APIRoute } from 'astro';
import { FASTAPI_BASE } from '../../lib/fastapi';

async function logoutAndRedirect(request: Request): Promise<Response> {
  const headers = new Headers({ Location: '/admin/login' });
  try {
    const upstream = await fetch(`${FASTAPI_BASE}/api/auth/logout`, {
      method: 'POST',
      headers: { cookie: request.headers.get('cookie') ?? '' },
    });
    const setCookie = upstream.headers.get('set-cookie');
    if (setCookie) headers.set('Set-Cookie', setCookie);
  } catch {
    // FastAPI unreachable — still redirect to /admin/login. Without the cookie
    // clear the user's session may linger until the FastAPI TTL elapses, but
    // returning a 5xx here would trap the user on /admin/logout indefinitely.
  }
  return new Response(null, { status: 302, headers });
}

export const GET: APIRoute = ({ request }) => logoutAndRedirect(request);
export const POST: APIRoute = ({ request }) => logoutAndRedirect(request);
