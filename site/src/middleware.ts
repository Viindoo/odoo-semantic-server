import { defineMiddleware } from 'astro:middleware';

export const onRequest = defineMiddleware(async (context, next) => {
  const path = context.url.pathname;
  if (!path.startsWith('/admin/')) return next();
  if (path === '/admin/login') return next();

  const verify = await fetch('http://localhost:8003/api/auth/verify', {
    headers: { cookie: context.request.headers.get('cookie') ?? '' },
  });
  if (!verify.ok) {
    return context.redirect('/admin/login');
  }
  return next();
});
