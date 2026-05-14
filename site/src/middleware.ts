import { defineMiddleware } from 'astro:middleware';
import { FASTAPI_BASE } from './lib/fastapi';

export const onRequest = defineMiddleware(async (context, next) => {
  const path = context.url.pathname;
  // /admin (no trailing slash) is a valid admin entry point too; the bare
  // `path.startsWith('/admin/')` test would let it through unauthenticated
  // and render the dashboard from SSR fallback data.
  if (path !== '/admin' && !path.startsWith('/admin/')) return next();
  if (path === '/admin/login') return next();

  // Network errors (FastAPI crashed, port closed) must redirect to login, NOT
  // bubble up as an unhandled 500 from Astro SSR. `fetch` throws on connection
  // refused, so we wrap in try/catch and treat failure as "unauthenticated".
  let verifyOk = false;
  try {
    const verify = await fetch(`${FASTAPI_BASE}/api/auth/verify`, {
      headers: { cookie: context.request.headers.get('cookie') ?? '' },
    });
    verifyOk = verify.ok;
  } catch {
    verifyOk = false;
  }
  if (!verifyOk) return context.redirect('/admin/login');
  return next();
});
