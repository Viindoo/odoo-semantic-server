import { defineMiddleware } from 'astro:middleware';
import { FASTAPI_BASE } from './lib/fastapi';

// Paths that are always public — no admin auth guard applied.
// /signup, /verify-email, /reset-password are public signup/auth flows (W-SG/W-UM).
const _PUBLIC_PATHS = new Set(['/signup', '/verify-email', '/reset-password']);

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

  // Public pages: never require admin auth
  if (_PUBLIC_PATHS.has(path)) return next();

  // /admin (no trailing slash) is a valid admin entry point too; the bare
  // `path.startsWith('/admin/')` test would let it through unauthenticated
  // and render the dashboard from SSR fallback data.
  if (path !== '/admin' && !path.startsWith('/admin/')) return next();
  if (path === '/admin/login') return next();

  const cookieHeader = context.request.headers.get('cookie') ?? '';

  // /admin/users/* requires admin privilege — redirect non-admins to dashboard.
  if (path === '/admin/users' || path.startsWith('/admin/users/')) {
    const adminPayload = await requireAdmin(cookieHeader);
    if (!adminPayload) {
      // Not logged in → redirect to login; logged in but not admin → 403 redirect to dashboard.
      const sessionPayload = await verifySession(cookieHeader);
      if (!sessionPayload || !sessionPayload.ok) return context.redirect('/admin/login');
      // Authenticated but not admin → dashboard with a flash (query param for UX)
      return context.redirect('/admin?error=admin_required');
    }
    return next();
  }

  // All other /admin/* paths: require authentication only.
  // Network errors (FastAPI crashed, port closed) must redirect to login, NOT
  // bubble up as an unhandled 500 from Astro SSR. `fetch` throws on connection
  // refused, so we wrap in try/catch and treat failure as "unauthenticated".
  const sessionPayload = await verifySession(cookieHeader);
  if (!sessionPayload || !sessionPayload.ok) return context.redirect('/admin/login');
  return next();
});
