// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * flash — SSOT toast/banner helper, shared by Astro inline scripts and React
 * islands.
 *
 * Mirrors the existing `showFlash` in `src/pages/admin/api-keys.astro`: a
 * fixed top-right banner that reuses any existing
 * `[data-testid="flash-banner"]` element when present, or lazily creates one
 * (idempotently — a single stable id) when the page has none.
 *
 * SSR-safe: a no-op when there is no `document` (Astro server render / Node).
 */

const BANNER_ID = 'osm-flash-banner';
const BASE_CLASS =
  'fixed top-4 right-4 z-50 px-5 py-3 rounded-xl shadow-lg text-sm font-medium border';
const SUCCESS_CLASS = 'bg-green-100 text-green-800 border-green-300';
const ERROR_CLASS = 'bg-red-100 text-red-800 border-red-300';

/** Locate the page's flash banner, creating a fixed top-right one if absent. */
function getOrCreateBanner(): HTMLElement {
  // Prefer an existing page-authored banner so we keep its wiring.
  const existing = document.querySelector(
    '[data-testid="flash-banner"]',
  ) as HTMLElement | null;
  if (existing) return existing;

  // Idempotent self-created banner — never duplicate.
  const prior = document.getElementById(BANNER_ID);
  if (prior) return prior;

  const el = document.createElement('div');
  el.id = BANNER_ID;
  el.setAttribute('data-testid', 'flash-banner');
  el.setAttribute('hidden', '');
  el.className = BASE_CLASS;
  document.body.appendChild(el);
  return el;
}

/**
 * Show a transient flash message.
 *
 * @param message   Text to display.
 * @param opts.error    `true` → red error styling; falsy → green success.
 * @param opts.timeoutMs Auto-hide delay in ms (default 4000).
 */
export function flash(
  message: string,
  opts: { error?: boolean; timeoutMs?: number } = {},
): void {
  // SSR guard — module may be imported during Astro server render.
  if (typeof document === 'undefined') return;

  const { error = false, timeoutMs = 4000 } = opts;
  const banner = getOrCreateBanner();

  // Announce to assistive tech (the pre-refactor inline helpers set these
  // dynamically on every flash; preserve that for any banner we drive).
  // Errors are assertive (interrupt), successes are polite.
  banner.setAttribute('role', error ? 'alert' : 'status');
  banner.setAttribute('aria-live', error ? 'assertive' : 'polite');
  banner.textContent = message;
  banner.className = `${BASE_CLASS} ${error ? ERROR_CLASS : SUCCESS_CLASS}`;
  banner.removeAttribute('hidden');

  setTimeout(() => banner.setAttribute('hidden', ''), timeoutMs);
}
