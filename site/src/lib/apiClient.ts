// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * apiClient — one-call JSON submit helper for UI call sites.
 *
 * Wraps the repeated pattern seen across admin islands and Astro inline
 * scripts:
 *   validate → withStepUp(() => fetch(url, { credentials:'include', ... }))
 *            → res.ok ? flash(...) : show(extractApiError(...))
 *
 * `submitJson` does the fetch + safe JSON parse + step-up wrapping + error
 * extraction, returning a uniform {ok, status, data, error} result. It does
 * NOT throw — neither on HTTP errors (the caller branches on `result.ok`) nor
 * on a network-level fetch rejection, which is folded into
 * `{ ok:false, status:0, error:<message> }` so call sites that do not wrap it
 * in try/catch never leak an unhandled rejection.
 */

import { extractApiError } from './apiError';
import { withStepUp } from './mfaStepUp';

export interface SubmitOpts extends Omit<RequestInit, 'body'> {
  /**
   * Request body. A plain object is JSON-serialized. A `Content-Type:
   * application/json` header is set by default for ALL requests (including
   * bodyless ones) unless the caller already set one OR the body is a
   * browser-managed binary type (FormData/Blob/...), which is passed through
   * untouched so the browser can set the multipart boundary.
   */
  body?: unknown;
  /** Wrap the fetch in the MFA step-up flow. Default `true`. */
  stepUp?: boolean;
  /** Error message when the body carries no readable error. Default `HTTP <status>`. */
  fallback?: string;
}

export interface SubmitResult<T = unknown> {
  ok: boolean;
  status: number;
  data: T;
  /** Human-readable error string when `ok` is false, else `null`. */
  error: string | null;
}

/**
 * Binary / browser-managed body types that must NEVER get a JSON Content-Type
 * (the browser sets the correct one — e.g. multipart boundary for FormData).
 */
function isBinaryBody(body: unknown): boolean {
  if (typeof body !== 'object' || body === null) return false;
  if (typeof FormData !== 'undefined' && body instanceof FormData) return true;
  if (typeof Blob !== 'undefined' && body instanceof Blob) return true;
  if (typeof ArrayBuffer !== 'undefined' && body instanceof ArrayBuffer) return true;
  if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) return true;
  if (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) return true;
  return false;
}

function isPlainObjectBody(body: unknown): boolean {
  if (body === null || body === undefined) return false;
  if (typeof body !== 'object') return false;
  return !isBinaryBody(body);
}

/**
 * Submit a JSON request and return a uniform result.
 *
 * - `credentials: 'include'` is the default (override via `opts.credentials`).
 * - A plain-object `body` is JSON-serialized with a JSON Content-Type header.
 * - `stepUp` (default true) wraps the fetch in {@link withStepUp} so a
 *   fresh-MFA 403 transparently opens the step-up modal and retries once.
 * - HTTP errors do NOT throw — inspect `result.ok` / `result.error`.
 * - A network failure does NOT throw either — it returns
 *   `{ ok:false, status:0, error:<message> }`.
 */
export async function submitJson<T = unknown>(
  url: string,
  opts: SubmitOpts = {},
): Promise<SubmitResult<T>> {
  const { body, stepUp = true, fallback, credentials, headers, ...rest } = opts;

  const finalHeaders = new Headers(headers as HeadersInit | undefined);
  let finalBody: BodyInit | undefined;

  if (isPlainObjectBody(body)) {
    finalBody = JSON.stringify(body);
  } else {
    // string | FormData | Blob | undefined → pass through untouched.
    finalBody = body as BodyInit | undefined;
  }

  // Default to a JSON Content-Type for everything EXCEPT browser-managed binary
  // bodies (FormData/Blob/...). This covers bodyless mutations (DELETE/POST with
  // no payload) too: Astro `security.checkOrigin` rejects a state-changing request
  // that lacks an application/json content-type as form-like (403 in dev/preview/CI).
  // Mirrors the always-JSON behavior of the pre-refactor apiFetch wrappers.
  if (!finalHeaders.has('Content-Type') && !isBinaryBody(body)) {
    finalHeaders.set('Content-Type', 'application/json');
  }

  const init: RequestInit = {
    credentials: credentials ?? 'include',
    headers: finalHeaders,
    ...rest,
  };
  if (finalBody !== undefined) init.body = finalBody;

  const doFetch = () => fetch(url, init);
  // A network-level failure (offline, DNS, connection reset, CORS) is folded
  // into a uniform { ok:false, status:0 } result — NOT re-thrown — so every call
  // site's `if (r.ok) … else …` surfaces it. The many call sites that (correctly)
  // do not wrap submitJson in try/catch would otherwise leak an unhandled
  // rejection, freezing the button in its loading state. status===0 lets a caller
  // distinguish a transport failure from an HTTP error response if it needs to.
  let res: Response;
  try {
    res = stepUp ? await withStepUp(doFetch) : await doFetch();
  } catch {
    return {
      ok: false,
      status: 0,
      data: {} as T,
      error: fallback ?? 'Network error - please check your connection and try again.',
    };
  }

  const data = (await res.json().catch(() => ({}))) as T;
  const error = res.ok
    ? null
    : extractApiError(data, fallback ?? `HTTP ${res.status}`);

  return { ok: res.ok, status: res.status, data, error };
}
