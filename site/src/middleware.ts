// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineMiddleware } from 'astro:middleware';
import { FASTAPI_BASE } from './lib/fastapi';

// Paths that are always public — no admin auth guard applied.
// /signup, /verify-email, /reset-password are public signup/auth flows (W-SG/W-UM).
const _PUBLIC_PATHS = new Set(['/signup', '/verify-email', '/reset-password']);

// Paths that load hCaptcha widget (third-party script + iframe + XHR origins).
// Currently only /signup conditionally loads `https://js.hcaptcha.com/1/api.js`
// when `PUBLIC_HCAPTCHA_SITE_KEY` is configured. If another page is ever
// wired up to hCaptcha, add it here AND verify the assertions in
// tests/browser/public/test_csp_headers.py still hold.
const _HCAPTCHA_PATHS = new Set(['/signup']);

/**
 * Build the default Content-Security-Policy directives for Astro SSR responses.
 *
 * https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Content-Security-Policy
 *
 * script-src 'self' 'unsafe-inline' — Astro SSR inlines <script> blocks from
 *   .astro pages as `<script type="module">…</script>` (no src= attribute) when
 *   the script is small enough that Vite's inlining threshold is met. Without
 *   'unsafe-inline', those inline module scripts are blocked and all click/submit
 *   handlers become no-ops (login-error stays hidden, API-key modal never opens,
 *   SSH-key banner never appears). 'self' alone only allows external /_astro/*.js
 *   files. TODO: migrate to per-request nonce injection once Astro exposes a
 *   first-class CSP nonce API so 'unsafe-inline' can be removed
 *   (TASKS.md M10 backlog).
 * style-src 'unsafe-inline' — Tailwind utility classes are often inlined at build time.
 * connect-src 'self' — React islands fetch /api/* via same-origin proxy.
 * form-action 'self' — OAuth redirect is browser navigation, NOT a form submit;
 *   form-action 'self' does not block it.
 */
function _defaultCspDirectives(): Record<string, string[]> {
  return {
    'default-src': ["'self'"],
    'script-src': ["'self'", "'unsafe-inline'"],
    'style-src': ["'self'", "'unsafe-inline'"],
    'img-src': ["'self'", 'data:', 'https:'],
    'font-src': ["'self'"],
    'connect-src': ["'self'"],
    'frame-src': ["'self'"],
    'frame-ancestors': ["'none'"],
    'base-uri': ["'self'"],
    'form-action': ["'self'"],
  };
}

/**
 * Build the per-path CSP string. Adds hCaptcha origins only for paths
 * registered in `_HCAPTCHA_PATHS`. Keeping the third-party allowlist
 * scoped (rather than blanket-granting it across the whole site) means
 * /admin/* and / never get to talk to js.hcaptcha.com — minimum
 * blast-radius for the hCaptcha-related script-src expansion.
 *
 * hCaptcha origins (https://docs.hcaptcha.com/configuration#content-security-policy-settings):
 *   - script-src   https://js.hcaptcha.com https://newassets.hcaptcha.com
 *   - connect-src  https://api.hcaptcha.com https://newassets.hcaptcha.com
 *   - frame-src    https://newassets.hcaptcha.com
 *   - style-src    already permits 'unsafe-inline' (hcaptcha widget needs)
 *   - img-src      already permits https: (hcaptcha widget assets)
 */
export function _buildCspForPath(pathname: string): string {
  const directives = _defaultCspDirectives();
  if (_HCAPTCHA_PATHS.has(pathname)) {
    directives['script-src'].push('https://js.hcaptcha.com', 'https://newassets.hcaptcha.com');
    directives['connect-src'].push('https://api.hcaptcha.com', 'https://newassets.hcaptcha.com');
    directives['frame-src'].push('https://newassets.hcaptcha.com');
  }
  return Object.entries(directives)
    .map(([name, values]) => `${name} ${values.join(' ')}`)
    .join('; ');
}

const _PERMISSIONS_POLICY =
  'accelerometer=(), camera=(), geolocation=(), gyroscope=(), ' +
  'magnetometer=(), microphone=(), payment=(), usb=()';

/**
 * Inject CSP + Permissions-Policy on every Astro SSR response.
 * https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Permissions-Policy
 */
function _addSecurityHeaders(response: Response, pathname: string): void {
  response.headers.set('Content-Security-Policy', _buildCspForPath(pathname));
  response.headers.set('Permissions-Policy', _PERMISSIONS_POLICY);
}

/**
 * Check if the current session is authenticated (any user).
 * Returns the verify JSON payload on success, null on failure.
 */
async function verifySession(cookieHeader: string): Promise<{ ok: boolean; username?: string; is_admin?: boolean } | null> {
  try {
    const res = await fetch(`${FASTAPI_BASE}/api/auth/verify`, {
      headers: { cookie: cookieHeader },
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/**
 * requireAdmin: check /api/auth/verify and verify is_admin: true.
 * Returns the verify payload if admin, null otherwise.
 * Used by /admin/users/* routes in the Astro middleware.
 */
async function requireAdmin(cookieHeader: string): Promise<{ ok: boolean; username?: string; is_admin?: boolean } | null> {
  const payload = await verifySession(cookieHeader);
  if (!payload || !payload.ok) return null;
  if (!payload.is_admin) return null;
  return payload;
}

export const onRequest = defineMiddleware(async (context, next) => {
  const path = context.url.pathname;

  // Local helper: wrap context.redirect() so the 3xx response also carries
  // CSP + Permissions-Policy. Security scanners flag 3xx responses without
  // these headers even though browsers will apply the destination page's
  // CSP after the redirect. Doing it once here keeps every redirect site
  // consistent.
  const _redirectWithHeaders = (location: string): Response => {
    const r = context.redirect(location);
    _addSecurityHeaders(r, path);
    return r;
  };

  // Public pages: never require admin auth — but always inject security headers.
  // Set locals.user = null for unauthenticated/public paths.
  if (_PUBLIC_PATHS.has(path)) {
    context.locals.user = null;
    const response = await next();
    _addSecurityHeaders(response, path);
    return response;
  }

  // /account/* is an authenticated self-service surface (My API Keys, etc.).
  // Anonymous users must be sent to /admin/login (single global login flow —
  // there is no separate /account/login). Authenticated users (admin OR
  // non-admin) pass through. Without this gate, anon hits /account/api-keys,
  // sees an empty self-service page, and clicks Generate Key only to get a
  // confusing 401 — a UX regression introduced when WI5 shipped /account/*.
  if (path === '/account' || path.startsWith('/account/')) {
    const cookieHeader = context.request.headers.get('cookie') ?? '';
    const sessionPayload = await verifySession(cookieHeader);
    if (!sessionPayload || !sessionPayload.ok) return _redirectWithHeaders('/admin/login');
    context.locals.user = {
      username: sessionPayload.username ?? 'unknown',
      is_admin: sessionPayload.is_admin ?? false,
    };
    const response = await next();
    _addSecurityHeaders(response, path);
    return response;
  }

  // /admin (no trailing slash) is a valid admin entry point too; the bare
  // `path.startsWith('/admin/')` test would let it through unauthenticated
  // and render the dashboard from SSR fallback data.
  if (path !== '/admin' && !path.startsWith('/admin/')) {
    // Prerendered routes (export const prerender = true) have no request
    // headers at build time — reading context.request.headers triggers an
    // Astro build warning. These public pages never render locals.user, so
    // skip the cookie/session lookup entirely. Security headers (_addSecurityHeaders)
    // are response headers, so they still apply via the early-return path.
    // See issue #140.
    if (context.isPrerendered) {
      context.locals.user = null;
      const response = await next();
      _addSecurityHeaders(response, path);
      return response;
    }
    // Non-admin SSR routes: populate locals.user if authenticated.
    const cookieHeader = context.request.headers.get('cookie') ?? '';
    const sessionPayload = await verifySession(cookieHeader);
    if (sessionPayload && sessionPayload.ok && sessionPayload.username) {
      context.locals.user = {
        username: sessionPayload.username,
        is_admin: sessionPayload.is_admin ?? false,
      };
    } else {
      context.locals.user = null;
    }
    const response = await next();
    _addSecurityHeaders(response, path);
    return response;
  }
  if (path === '/admin/login' || path === '/admin/logout') {
    context.locals.user = null;
    const response = await next();
    _addSecurityHeaders(response, path);
    return response;
  }

  const cookieHeader = context.request.headers.get('cookie') ?? '';

  // /admin/users/* requires admin privilege — redirect non-admins to dashboard.
  if (path === '/admin/users' || path.startsWith('/admin/users/')) {
    const adminPayload = await requireAdmin(cookieHeader);
    if (!adminPayload) {
      // Not logged in → redirect to login; logged in but not admin → 403 redirect to dashboard.
      const sessionPayload = await verifySession(cookieHeader);
      if (!sessionPayload || !sessionPayload.ok) return _redirectWithHeaders('/admin/login');
      // Authenticated but not admin → dashboard with a flash (query param for UX)
      return _redirectWithHeaders('/admin?error=admin_required');
    }
    context.locals.user = {
      username: adminPayload.username!,
      is_admin: true,
    };
    const response = await next();
    _addSecurityHeaders(response, path);
    return response;
  }

  // All other /admin/* paths: require authentication only.
  // Network errors (FastAPI crashed, port closed) must redirect to login, NOT
  // bubble up as an unhandled 500 from Astro SSR. `fetch` throws on connection
  // refused, so we wrap in try/catch and treat failure as "unauthenticated".
  const sessionPayload = await verifySession(cookieHeader);
  if (!sessionPayload || !sessionPayload.ok) return _redirectWithHeaders('/admin/login');

  // Populate locals.user from the verify payload.
  context.locals.user = {
    username: sessionPayload.username ?? 'unknown',
    is_admin: sessionPayload.is_admin ?? false,
  };

  // Non-admin users hitting /admin/* (except auth pages and /admin/users/* handled above)
  // are redirected to their own account page rather than seeing an admin-only UI.
  if (
    sessionPayload.is_admin === false &&
    path.startsWith('/admin/') &&
    !path.startsWith('/admin/auth/')  // forward-compat: reserved for future OAuth callback routes
  ) {
    return _redirectWithHeaders('/account/api-keys');
  }

  const response = await next();
  _addSecurityHeaders(response, path);
  return response;
});
