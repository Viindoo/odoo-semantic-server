// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * mfaStepUp — MFA step-up infra for admin mutation call sites (W4)
 *
 * ## Event-bridge contract (for W5 and StepUpMfaModal)
 *
 * When `withStepUp` intercepts a 403 "Fresh MFA required" response it fires:
 *
 *   window.dispatchEvent(new CustomEvent('osm:mfa-step-up', {
 *     detail: { resolve: (ok: boolean) => void }
 *   }));
 *
 * `StepUpMfaModal` listens for this event in a `useEffect`, renders the
 * modal, and calls `detail.resolve(true)` on successful verify or
 * `detail.resolve(false)` on Cancel.
 *
 * Only one step-up dialog is open at any time: if a second request arrives
 * while one is already pending, the new request enqueues its resolver onto a
 * shared waiters array (the modal is not re-opened). When the user verifies or
 * cancels, ALL queued waiters resolve with the same `ok` — no caller is left
 * hanging.
 *
 * ## Usage for W5 call sites
 *
 *   import { withStepUp } from '../lib/mfaStepUp';
 *
 *   const res = await withStepUp(() =>
 *     fetch('/api/admin/something', {
 *       method: 'PATCH',
 *       credentials: 'include',
 *       headers: { 'Content-Type': 'application/json' },
 *       body: JSON.stringify(payload),
 *     })
 *   );
 *   if (!res.ok) { ... } // normal error handling, including 403 if user cancelled
 *
 * ## Mounting requirement
 *
 * `<StepUpMfaModal client:load />` MUST be mounted once in
 * `site/src/layouts/AdminLayout.astro` (before `</BaseLayout>`).
 * It is a zero-render host — nothing is visible until a step-up is requested.
 */

import { extractApiError } from './apiError';

// ---------------------------------------------------------------------------
// Sentinel
// ---------------------------------------------------------------------------

/**
 * Stable machine-readable discriminator the backend sets in the 403 response
 * detail (`detail.error`) when the MFA freshness window has expired. This is
 * the PRIMARY signal — match `data.detail.error === STEP_UP_ERROR_CODE`.
 *
 * See `src/web_ui/auth.py::_check_mfa_freshness` (ADR-0043 D5).
 */
export const STEP_UP_ERROR_CODE = 'mfa_freshness_required';

/**
 * Human-readable substring the backend keeps in the 403 detail message for
 * back-compat. Used only as a FALLBACK when the structured `error` field is
 * absent (e.g. an older backend or a plain-string detail).
 */
export const FRESH_MFA_SENTINEL = 'Fresh MFA required';

// ---------------------------------------------------------------------------
// Low-level API call
// ---------------------------------------------------------------------------

export interface StepUpResult {
  ok: boolean;
  error?: string;
}

/**
 * POST /api/auth/totp/step-up with either a 6-digit `code` (TOTP) or a
 * `backup_code` (one-time recovery code).
 *
 * Returns `{ ok: true }` on success, `{ ok: false, error: string }` on
 * failure.  Never throws — network errors are caught and returned as `error`.
 */
export async function stepUpVerify(
  payload: { code: string } | { backup_code: string },
): Promise<StepUpResult> {
  try {
    const res = await fetch('/api/auth/totp/step-up', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) return { ok: true };
    const data = await res.json().catch(() => ({}));
    // extractApiError handles string / object / pydantic-list detail shapes so a
    // 422 (e.g. neither code nor backup_code) never surfaces "[object Object]".
    return { ok: false, error: extractApiError(data, `HTTP ${res.status}`) };
  } catch (e: unknown) {
    return { ok: false, error: String(e) };
  }
}

// ---------------------------------------------------------------------------
// Module-level event bridge (SSR-safe)
// ---------------------------------------------------------------------------

/**
 * Queue of resolvers for callers awaiting the SAME in-flight step-up modal.
 *
 * The first caller (array empty → non-empty) dispatches the CustomEvent with a
 * STABLE drain callback; subsequent concurrent callers merely push their
 * resolver. When the modal resolves, every queued waiter is settled with the
 * same `ok` value and the array is cleared, so a later (separate) step-up works.
 */
let _waiters: Array<(ok: boolean) => void> = [];

/**
 * Drain ALL queued waiters with `ok`, then clear the queue.
 *
 * This is the single, stable callback handed to the modal via the CustomEvent.
 * It is safe to call exactly once per modal cycle; a defensive snapshot guards
 * against re-entrancy (a waiter that itself enqueues a new request).
 */
function _drainWaiters(ok: boolean): void {
  const pending = _waiters;
  _waiters = [];
  for (const resolve of pending) resolve(ok);
}

/**
 * Hydration-race guard. `requestStepUp` parks a resolver in `_waiters` and fires
 * a ONE-SHOT `osm:mfa-step-up` CustomEvent. If the StepUpMfaModal island (also
 * `client:load`) had not yet hydrated and attached its listener when the event
 * fired, the event is dropped and the parked resolver would hang forever (the
 * caller's `withStepUp` never returns → the Save button stays stuck on
 * "Saving…"). The modal calls this on mount to CLAIM any already-pending request
 * and open itself. Returns the drain callback to use as the modal's resolve, or
 * null if nothing is pending.
 */
export function claimPendingStepUp(): ((ok: boolean) => void) | null {
  return _waiters.length > 0 ? _drainWaiters : null;
}

/**
 * Open the MFA step-up modal (or join the in-flight one) and await the
 * user's decision.
 *
 * Returns `true` if TOTP was verified successfully, `false` if the user
 * cancelled or if the browser is not in a window context (SSR guard).
 *
 * Concurrency: multiple near-simultaneous callers coalesce onto a single modal;
 * all of them resolve with the same result when the user verifies or cancels.
 *
 * Called internally by `withStepUp`.  Exposed for testing and for any
 * advanced W5 call site that needs direct control.
 */
export function requestStepUp(): Promise<boolean> {
  // SSR guard — this module may be imported in Node context during Astro SSR
  if (typeof window === 'undefined') return Promise.resolve(false);

  return new Promise<boolean>((resolve) => {
    const wasIdle = _waiters.length === 0;
    _waiters.push(resolve);

    // Only the FIRST caller (empty → non-empty transition) opens the modal.
    if (wasIdle) {
      window.dispatchEvent(
        new CustomEvent('osm:mfa-step-up', {
          detail: { resolve: _drainWaiters },
        }),
      );
    }
  });
}

// ---------------------------------------------------------------------------
// High-level wrapper
// ---------------------------------------------------------------------------

/**
 * Wrap any fetch call with automatic MFA step-up on `403 Fresh MFA required`.
 *
 * Flow:
 *   1. Execute `doFetch()`.
 *   2. If response is 403 and signals fresh-MFA (stable `detail.error ===
 *      STEP_UP_ERROR_CODE`, with `FRESH_MFA_SENTINEL` substring fallback):
 *      a. Open the step-up modal (fires `osm:mfa-step-up` CustomEvent).
 *      b. If user verifies successfully → retry `doFetch()` once and return.
 *      c. If user cancels → return the ORIGINAL 403 response so the caller's
 *         own error-handling path runs normally.
 *   3. Otherwise return the response unchanged.
 *
 * Body-consumption safety: the 403 response is inspected via `res.clone()`
 * so the original body stream remains intact for the caller.
 */
export async function withStepUp(
  doFetch: () => Promise<Response>,
): Promise<Response> {
  const res = await doFetch();

  if (res.status !== 403) return res;

  // Inspect body without consuming the original stream.
  //
  // Detection prefers the stable machine-readable code (`detail.error ===
  // STEP_UP_ERROR_CODE`); the human-string substring is kept as a fallback for
  // back-compat with a plain-string detail or an older backend (F5).
  let isFreshMfaRequired = false;
  try {
    const cloned = res.clone();
    const data = await cloned.json() as Record<string, unknown>;
    const detail = data.detail;
    if (detail && typeof detail === 'object') {
      const obj = detail as Record<string, unknown>;
      isFreshMfaRequired =
        obj.error === STEP_UP_ERROR_CODE ||
        String(obj.message ?? '').includes(FRESH_MFA_SENTINEL);
    } else {
      // Plain-string detail (back-compat fallback)
      isFreshMfaRequired = String(detail ?? '').includes(FRESH_MFA_SENTINEL);
    }
  } catch {
    // Non-JSON 403 — not a step-up gate; fall through
  }

  if (!isFreshMfaRequired) return res;

  const verified = await requestStepUp();
  if (!verified) {
    // User cancelled — return original response so caller can handle it
    return res;
  }

  // Retry once after successful step-up
  return doFetch();
}
