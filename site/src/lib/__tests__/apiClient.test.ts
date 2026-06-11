// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for submitJson — the one-call JSON submit helper.
 *
 * Business rules under protection:
 *   - On a 2xx, `ok` is true and `error` is null.
 *   - On an HTTP error with an object detail, `error` is the readable message
 *     (NOT "[object Object]"); the call does NOT throw.
 *   - credentials:'include' is sent by default.
 *   - A plain-object body is JSON-serialized with a JSON Content-Type header.
 *   - stepUp (default) routes the fetch through withStepUp.
 *   - A bodyless mutation still carries a JSON Content-Type (Astro checkOrigin).
 *   - A network failure is folded into { ok:false, status:0 } — it does NOT throw.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// withStepUp is mocked so we can both (a) keep fetch behaviour transparent and
// (b) assert it was invoked. The default impl just calls the passed fetch fn.
vi.mock('../mfaStepUp', () => ({
  withStepUp: vi.fn((doFetch: () => Promise<Response>) => doFetch()),
}));

import { submitJson } from '../apiClient';
import { withStepUp } from '../mfaStepUp';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('submitJson', () => {
  it('(a) returns ok=true and error=null on a 2xx response', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { value: 7 }));
    const res = await submitJson<{ value: number }>('/api/x');
    expect(res.ok).toBe(true);
    expect(res.status).toBe(200);
    expect(res.data.value).toBe(7);
    expect(res.error).toBeNull();
  });

  it('(b) surfaces a readable error for an object-detail HTTP error', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(403, {
        detail: { error: 'mfa_freshness_required', message: 'Re-verify MFA' },
      }),
    );
    const res = await submitJson('/api/x', { method: 'POST' });
    expect(res.ok).toBe(false);
    expect(res.status).toBe(403);
    expect(res.error).toBe('Re-verify MFA');
    expect(res.error).not.toContain('[object Object]');
  });

  it('(c) sends credentials:include by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await submitJson('/api/x');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe('include');
  });

  it('(d) JSON-serializes a plain object body with a JSON Content-Type', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await submitJson('/api/x', { method: 'POST', body: { a: 1 } });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.body).toBe(JSON.stringify({ a: 1 }));
    const headers = new Headers(init.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('(d2) does NOT set Content-Type for a FormData body', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    const fd = new FormData();
    fd.append('f', 'v');
    await submitJson('/api/x', { method: 'POST', body: fd });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.body).toBe(fd);
    const headers = new Headers(init.headers);
    expect(headers.get('Content-Type')).toBeNull();
  });

  it('(d3) sets a JSON Content-Type on a bodyless mutation (Astro checkOrigin)', async () => {
    // A DELETE/POST with no payload must still carry application/json so Astro
    // security.checkOrigin does not 403 it as a form-like request.
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await submitJson('/api/x', { method: 'DELETE' });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.body).toBeUndefined();
    const headers = new Headers(init.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('(e) routes through withStepUp by default', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await submitJson('/api/x');
    expect(vi.mocked(withStepUp)).toHaveBeenCalledTimes(1);
  });

  it('(e2) bypasses withStepUp when stepUp:false', async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await submitJson('/api/x', { stepUp: false });
    expect(vi.mocked(withStepUp)).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('(f) folds a network rejection into ok:false/status:0 (does NOT throw)', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const res = await submitJson('/api/x', { stepUp: false });
    expect(res.ok).toBe(false);
    expect(res.status).toBe(0);
    expect(typeof res.error).toBe('string');
    expect(res.error).not.toContain('[object Object]');
  });

  it('(f2) uses the caller fallback as the network-error message', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    const res = await submitJson('/api/x', { stepUp: false, fallback: 'Could not save.' });
    expect(res.error).toBe('Could not save.');
  });

  it('falls back to "HTTP <status>" when an error body has no readable error', async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, {}));
    const res = await submitJson('/api/x');
    expect(res.error).toBe('HTTP 500');
  });
});
