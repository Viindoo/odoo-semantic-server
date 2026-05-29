// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Unit tests for mfaStepUp.ts
 *
 * Environment: happy-dom (via vitest.config.ts) — supplies window, CustomEvent,
 * fetch-compatible Response / globalThis.fetch.
 *
 * We test the three exported functions:
 *   withStepUp   — high-level wrapper: 403+sentinel → event + retry; cancel → original 403;
 *                  non-sentinel 403 → passthrough; non-403 → passthrough.
 *   stepUpVerify — low-level POST; success → {ok:true}; failure → {ok:false, error}.
 *   requestStepUp — SSR guard + singleton-event coalescing.
 *
 * Module-level singleton (_pendingResolve) isolation:
 *   vi.resetModules() in beforeEach ensures each test gets a fresh module import
 *   with _pendingResolve = null.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal JSON Response for fetch mocks. */
function makeJsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Listen once for the next `osm:mfa-step-up` CustomEvent and return the
 * resolve callback from its detail.
 */
function captureNextStepUpResolve(): Promise<(ok: boolean) => void> {
  return new Promise((res) => {
    window.addEventListener(
      'osm:mfa-step-up',
      (e) => {
        const evt = e as CustomEvent<{ resolve: (ok: boolean) => void }>;
        res(evt.detail.resolve);
      },
      { once: true },
    );
  });
}

// ---------------------------------------------------------------------------
// withStepUp
// ---------------------------------------------------------------------------

describe('withStepUp', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('passes 200 responses straight through without firing an event', async () => {
    const { withStepUp } = await import('../mfaStepUp');
    const ok200 = makeJsonResponse(200, { data: 'ok' });
    const doFetch = vi.fn().mockResolvedValue(ok200);

    let eventFired = false;
    window.addEventListener('osm:mfa-step-up', () => { eventFired = true; }, { once: true });

    const result = await withStepUp(doFetch);

    expect(result.status).toBe(200);
    expect(doFetch).toHaveBeenCalledTimes(1);
    expect(eventFired).toBe(false);
  });

  it('passes a 403 WITHOUT FRESH_MFA_SENTINEL straight through without firing an event', async () => {
    const { withStepUp } = await import('../mfaStepUp');
    const forbidden = makeJsonResponse(403, { detail: 'some other forbidden reason' });
    const doFetch = vi.fn().mockResolvedValue(forbidden);

    let eventFired = false;
    window.addEventListener('osm:mfa-step-up', () => { eventFired = true; }, { once: true });

    const result = await withStepUp(doFetch);

    expect(result.status).toBe(403);
    expect(doFetch).toHaveBeenCalledTimes(1);
    expect(eventFired).toBe(false);
  });

  it('passes a non-JSON 403 straight through without firing an event', async () => {
    const { withStepUp } = await import('../mfaStepUp');
    const nonJson = new Response('Forbidden', {
      status: 403,
      headers: { 'Content-Type': 'text/plain' },
    });
    const doFetch = vi.fn().mockResolvedValue(nonJson);

    let eventFired = false;
    window.addEventListener('osm:mfa-step-up', () => { eventFired = true; }, { once: true });

    const result = await withStepUp(doFetch);

    expect(result.status).toBe(403);
    expect(doFetch).toHaveBeenCalledTimes(1);
    expect(eventFired).toBe(false);
  });

  it('fires osm:mfa-step-up event and retries on 403+FRESH_MFA_SENTINEL when user verifies', async () => {
    const { withStepUp, FRESH_MFA_SENTINEL } = await import('../mfaStepUp');

    const forbidden = makeJsonResponse(403, { detail: FRESH_MFA_SENTINEL });
    const retried200 = makeJsonResponse(200, { data: 'retry-ok' });
    const doFetch = vi.fn()
      .mockResolvedValueOnce(forbidden)    // first call → 403 step-up gate
      .mockResolvedValueOnce(retried200);  // second call (retry after verify) → 200

    // Capture the event's resolve, then immediately resolve(true) (user verified)
    const eventCapture = captureNextStepUpResolve().then((resolve) => resolve(true));

    const result = await withStepUp(doFetch);
    await eventCapture; // ensure promise chain fully settles

    expect(doFetch).toHaveBeenCalledTimes(2); // initial + retry
    expect(result.status).toBe(200);
  });

  it('returns original 403 (no retry) when user cancels the step-up modal', async () => {
    const { withStepUp, FRESH_MFA_SENTINEL } = await import('../mfaStepUp');

    const forbidden = makeJsonResponse(403, { detail: FRESH_MFA_SENTINEL });
    const doFetch = vi.fn().mockResolvedValue(forbidden);

    // Capture event resolve, call with false (user cancelled)
    const eventCapture = captureNextStepUpResolve().then((resolve) => resolve(false));

    const result = await withStepUp(doFetch);
    await eventCapture;

    expect(doFetch).toHaveBeenCalledTimes(1); // no retry — cancelled
    expect(result.status).toBe(403);
  });

  it('does not consume the original body stream (clones before parsing)', async () => {
    // The 403 body must remain readable by the caller even after withStepUp peeks
    // at it via res.clone().json()  — user-cancel path returns the original response.
    const { withStepUp, FRESH_MFA_SENTINEL } = await import('../mfaStepUp');

    const forbidden = makeJsonResponse(403, { detail: FRESH_MFA_SENTINEL });
    const doFetch = vi.fn().mockResolvedValue(forbidden);

    const eventCapture = captureNextStepUpResolve().then((resolve) => resolve(false));

    const result = await withStepUp(doFetch);
    await eventCapture;

    // Original body should still be readable (not consumed by clone inspection)
    const body = await result.json() as { detail: string };
    expect(body.detail).toBe(FRESH_MFA_SENTINEL);
  });
});

// ---------------------------------------------------------------------------
// stepUpVerify
// ---------------------------------------------------------------------------

describe('stepUpVerify', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('returns {ok:true} when POST /api/auth/totp/step-up responds 200', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200 })));

    const result = await stepUpVerify({ code: '123456' });

    expect(result.ok).toBe(true);
    expect(result.error).toBeUndefined();

    vi.unstubAllGlobals();
  });

  it('POSTs to /api/auth/totp/step-up with the TOTP code payload', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    await stepUpVerify({ code: '654321' });

    expect(mockFetch).toHaveBeenCalledOnce();
    const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/auth/totp/step-up');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ code: '654321' });

    vi.unstubAllGlobals();
  });

  it('POSTs with backup_code payload when a backup code is supplied', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    const mockFetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    await stepUpVerify({ backup_code: 'ABCD-1234' });

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ backup_code: 'ABCD-1234' });

    vi.unstubAllGlobals();
  });

  it('returns {ok:false, error} from detail field on non-200 JSON response', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        makeJsonResponse(422, { detail: 'invalid_code' }),
      ),
    );

    const result = await stepUpVerify({ code: '000000' });

    expect(result.ok).toBe(false);
    expect(result.error).toBe('invalid_code');

    vi.unstubAllGlobals();
  });

  it('returns {ok:false, error} from error field when detail is absent', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        makeJsonResponse(422, { error: 'totp_not_setup' }),
      ),
    );

    const result = await stepUpVerify({ backup_code: 'XXXXXXXX' });

    expect(result.ok).toBe(false);
    expect(result.error).toBe('totp_not_setup');

    vi.unstubAllGlobals();
  });

  it('returns HTTP status string as error when response body is not JSON', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('Bad Gateway', { status: 503 })),
    );

    const result = await stepUpVerify({ code: '123456' });

    expect(result.ok).toBe(false);
    expect(result.error).toBe('HTTP 503');

    vi.unstubAllGlobals();
  });

  it('catches network errors and returns {ok:false, error} (never throws)', async () => {
    const { stepUpVerify } = await import('../mfaStepUp');

    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    );

    const result = await stepUpVerify({ code: '123456' });

    expect(result.ok).toBe(false);
    expect(result.error).toContain('Failed to fetch');

    vi.unstubAllGlobals();
  });
});

// ---------------------------------------------------------------------------
// requestStepUp
// ---------------------------------------------------------------------------

describe('requestStepUp', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('returns false immediately in SSR context (window undefined guard)', async () => {
    // Temporarily remove window to simulate SSR / Node context
    const origWindow = globalThis.window;
    // @ts-expect-error — intentional SSR simulation
    delete globalThis.window;

    const { requestStepUp } = await import('../mfaStepUp');
    const result = await requestStepUp();
    expect(result).toBe(false);

    // Restore
    globalThis.window = origWindow;
  });

  it('dispatches exactly one osm:mfa-step-up event even with two concurrent calls', async () => {
    // This test verifies the singleton guard: a second requestStepUp() call
    // while one is already pending must NOT dispatch a second event.
    // It does NOT verify that both promises resolve (that requires calling
    // the module-internal _pendingResolve chain, which is not exported).
    // The full end-to-end resolve flow is covered by withStepUp tests above.
    const { requestStepUp } = await import('../mfaStepUp');

    let eventCount = 0;
    let firstResolve: ((ok: boolean) => void) | null = null;

    window.addEventListener('osm:mfa-step-up', (e) => {
      eventCount++;
      const evt = e as CustomEvent<{ resolve: (ok: boolean) => void }>;
      // Capture the resolve from the FIRST (and only expected) event.
      // NOTE: after p2 runs, the module's _pendingResolve is overwritten to a
      // chain function; calling firstResolve here only settles p1, not p2.
      // We deliberately don't await p2 to avoid hanging the test.
      firstResolve = evt.detail.resolve;
    });

    const p1 = requestStepUp();
    const p2 = requestStepUp(); // should coalesce, not fire a new event

    // Invariant: only ONE event dispatched regardless of how many concurrent calls
    expect(eventCount).toBe(1);
    expect(firstResolve).not.toBeNull();

    // Settle p1 so the test does not leak a pending promise.
    // p2 stays pending (its resolve chain requires calling the module-internal
    // _pendingResolve which was overwritten after p2 registered). This is an
    // acceptable limitation of unit-testing a non-exported module singleton.
    firstResolve!(true);
    const r1 = await p1;
    expect(r1).toBe(true);

    // Discard p2 reference (it will be GC'd; resetModules clears module state)
    void p2;
  });
});
