// SPDX-License-Identifier: AGPL-3.0-or-later
// SSR proxy: forwards /api/* to FastAPI when Astro is the only origin.
//
// In production, nginx routes /api/* directly to FastAPI per docs/deploy/nginx-m8.conf,
// so this proxy never runs. In `pnpm preview` (CI browser tests) and `pnpm dev`,
// nginx is absent — without this proxy, client-side fetch('/api/...') hits Astro
// (which has no /api/* page) and 404s. The Vite dev-server proxy works in `pnpm dev`
// only and is silently dropped by `pnpm preview`; this SSR handler covers both.
//
// IMPORTANT — Astro `security.checkOrigin` interaction:
// Astro 5.x enables `security.checkOrigin: true` by default. The guard rejects
// POST/DELETE/PATCH requests whose Content-Type is missing OR is one of the
// form-encoding values (application/x-www-form-urlencoded, multipart/form-data,
// text/plain) with a 403 "Cross-site form submission" — even when the request
// is same-origin. JSON requests (`Content-Type: application/json`) are exempt.
// All client-side mutation fetches in this app MUST set
// `headers: { 'Content-Type': 'application/json' }`, otherwise this endpoint
// returns 403 before the handler below ever runs. Symptom we hit in browser
// tests: delete buttons looked broken (no reload, page stale), because the
// proxy short-circuited at the framework layer.
import type { APIRoute } from 'astro';
import { FASTAPI_BASE } from '../../lib/fastapi';

const HOP_BY_HOP = new Set([
  'host',
  'connection',
  'transfer-encoding',
  'keep-alive',
  'upgrade',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
]);

const proxy: APIRoute = async ({ request, params }) => {
  const subpath = Array.isArray(params.path)
    ? params.path.join('/')
    : (params.path ?? '');
  const url = new URL(request.url);
  const target = `${FASTAPI_BASE}/api/${subpath}${url.search}`;

  const forwardHeaders = new Headers();
  for (const [k, v] of request.headers) {
    if (!HOP_BY_HOP.has(k.toLowerCase())) forwardHeaders.set(k, v);
  }

  let body: BodyInit | undefined;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    body = await request.arrayBuffer();
  }

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: forwardHeaders,
      body,
      redirect: 'manual',
    });
    const respHeaders = new Headers();
    for (const [k, v] of upstream.headers) {
      if (!HOP_BY_HOP.has(k.toLowerCase())) respHeaders.set(k, v);
    }
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: respHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({
        error: 'upstream_unavailable',
        detail: err instanceof Error ? err.message : String(err),
      }),
      { status: 502, headers: { 'Content-Type': 'application/json' } },
    );
  }
};

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
