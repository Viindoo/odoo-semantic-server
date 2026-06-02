// SPDX-License-Identifier: AGPL-3.0-or-later
// SSR proxy: forwards /api/* to FastAPI when Astro is the only origin.
//
// In production, nginx routes /api/* directly to FastAPI per docs/deploy/nginx-m8.conf,
// so this proxy never runs. In `pnpm preview` (CI browser tests) and `pnpm dev`,
// nginx is absent — without this proxy, client-side fetch('/api/...') hits Astro
// (which has no /api/* page) and 404s. The Vite dev-server proxy works in `pnpm dev`
// only and is silently dropped by `pnpm preview`; this SSR handler covers both.
//
// IMPORTANT — Astro 6.3.3 `security.checkOrigin` interaction (issue #236):
// Astro 6.x enables `security.checkOrigin: true` by default. The guard compares
// the request Origin header against url.origin (derived from the Host header).
// When `security.allowedDomains` is empty, Astro falls back to "localhost" as the
// hostname, so url.origin becomes "http://localhost:4321" even when the server is
// bound to 127.0.0.1:4321. Browser fetches from http://127.0.0.1:4321 send
// Origin: http://127.0.0.1:4321, which does NOT match "http://localhost:4321".
// The mismatch causes 403 for any multipart/form-data or form-urlencoded request
// (these are "form-like" in Astro's heuristic). JSON requests are unaffected.
//
// The restore upload (POST /api/operations/restore) uses multipart/form-data,
// so it hit this 403 in dev/preview. Fix: ASTRO_DEV_ORIGIN must be set at BUILD TIME
// so astro.config.mjs can bake the correct allowedDomains into the output.
//   pnpm dev          → env set in script → config re-evaluated on restart → OK
//   pnpm build:dev    → env set in script → baked into build → pnpm preview works
//   pnpm preview:dev  → convenience alias for build:dev + preview
//   pnpm build        → env unset → prod default (allowedDomains empty) → OK in prod
// checkOrigin stays enabled in all environments.
// See ADR-0019 §Dev/Preview Origin Mismatch and astro.config.mjs for full details.
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
