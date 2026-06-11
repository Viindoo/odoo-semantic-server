// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for flash — the SSOT toast/banner helper.
 *
 * Environment: happy-dom (vitest.config.ts) supplies document/window.
 *
 * Business rules under protection:
 *   - flash fills a banner with the message text.
 *   - error:true applies the red error class; default applies the green one.
 *   - the same banner element is reused (no duplicate banners created).
 *   - SSR guard: with no document, flash is a no-op (does not throw).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flash } from '../flash';

beforeEach(() => {
  document.body.innerHTML = '';
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function banner(): HTMLElement | null {
  return document.querySelector('[data-testid="flash-banner"]');
}

describe('flash', () => {
  it('creates a banner and fills it with the message', () => {
    flash('Saved.');
    const el = banner();
    expect(el).not.toBeNull();
    expect(el!.textContent).toBe('Saved.');
    expect(el!.hasAttribute('hidden')).toBe(false);
  });

  it('applies the green success class by default', () => {
    flash('OK');
    expect(banner()!.className).toContain('bg-green-100');
    expect(banner()!.className).not.toContain('bg-red-100');
  });

  it('applies the red error class when error:true', () => {
    flash('Nope', { error: true });
    expect(banner()!.className).toContain('bg-red-100');
    expect(banner()!.className).not.toContain('bg-green-100');
  });

  it('reuses an existing page-authored banner rather than duplicating', () => {
    const existing = document.createElement('div');
    existing.setAttribute('data-testid', 'flash-banner');
    existing.setAttribute('hidden', '');
    document.body.appendChild(existing);

    flash('first');
    flash('second');

    const all = document.querySelectorAll('[data-testid="flash-banner"]');
    expect(all.length).toBe(1);
    expect(all[0]).toBe(existing);
    expect(existing.textContent).toBe('second');
  });

  it('does not create a second banner across repeated calls', () => {
    flash('one');
    flash('two');
    expect(document.querySelectorAll('[data-testid="flash-banner"]').length).toBe(1);
  });

  it('auto-hides after the timeout', () => {
    flash('bye', { timeoutMs: 1000 });
    expect(banner()!.hasAttribute('hidden')).toBe(false);
    vi.advanceTimersByTime(1000);
    expect(banner()!.hasAttribute('hidden')).toBe(true);
  });
});

describe('flash — SSR guard', () => {
  it('is a no-op (no throw) when document is undefined', () => {
    const realDoc = globalThis.document;
    // Simulate a Node/SSR context with no document.
    // @ts-expect-error — intentionally removing for the SSR-guard test.
    delete globalThis.document;
    try {
      expect(() => flash('ssr')).not.toThrow();
    } finally {
      globalThis.document = realDoc;
    }
  });
});
