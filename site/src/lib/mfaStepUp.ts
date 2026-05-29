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
 * while one is already pending, the new request waits for the same Promise
 * (i.e. the modal is not re-opened — the singleton `_pendingResolve` guard
 * prevents it).
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

// ---------------------------------------------------------------------------
// Sentinel
// ---------------------------------------------------------------------------

/**
 * Substring the backend includes in the 403 `detail` field when the MFA
 * freshness window has expired.  Match against `detail.includes(...)`.
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
    const data = await res.json().catch(() => ({})) as Record<string, unknown>;
    const err = String(data.error ?? data.detail ?? `HTTP ${res.status}`);
    return { ok: false, error: err };
  } catch (e: unknown) {
    return { ok: false, error: String(e) };
  }
}

// ---------------------------------------------------------------------------
// Module-level event bridge (SSR-safe)
// ---------------------------------------------------------------------------

/** Resolve callback kept by `requestStepUp` until the modal calls it. */
let _pendingResolve: ((ok: boolean) => void) | null = null;

/**
 * Open the MFA step-up modal (or reuse the in-flight one) and await the
 * user's decision.
 *
 * Returns `true` if TOTP was verified successfully, `false` if the user
 * cancelled or if the browser is not in a window context (SSR guard).
 *
 * Called internally by `withStepUp`.  Exposed for testing and for any
 * advanced W5 call site that needs direct control.
 */
export function requestStepUp(): Promise<boolean> {
  // SSR guard — this module may be imported in Node context during Astro SSR
  if (typeof window === 'undefined') return Promise.resolve(false);

  // Coalesce concurrent requests onto the same Promise
  if (_pendingResolve !== null) {
    return new Promise<boolean>((resolve) => {
      const originalResolve = _pendingResolve!;
      _pendingResolve = (ok: boolean) => {
        originalResolve(ok);
        resolve(ok);
      };
    });
  }

  return new Promise<boolean>((resolve) => {
    _pendingResolve = (ok: boolean) => {
      _pendingResolve = null;
      resolve(ok);
    };
    window.dispatchEvent(
      new CustomEvent('osm:mfa-step-up', {
        detail: { resolve: _pendingResolve },
      }),
    );
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
 *   2. If response is 403 and contains `FRESH_MFA_SENTINEL` in `detail`:
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

  // Inspect body without consuming the original stream
  let isFreshMfaRequired = false;
  try {
    const cloned = res.clone();
    const data = await cloned.json() as Record<string, unknown>;
    const detail = String(data.detail ?? '');
    isFreshMfaRequired = detail.includes(FRESH_MFA_SENTINEL);
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
