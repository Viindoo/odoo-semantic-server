// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for extractApiError — the SSOT for rendering a FastAPI error body.
 *
 * Business rule under protection: a user-facing error must ALWAYS be a
 * readable string and must NEVER render "[object Object]", regardless of
 * whether the backend returns `detail` as a string, an object (MFA 403), or a
 * pydantic-422 validation list.
 */

import { describe, expect, it } from 'vitest';
import { extractApiError } from '../apiError';

const FALLBACK = 'Something went wrong.';

describe('extractApiError — shape coverage', () => {
  it('returns a plain string detail verbatim', () => {
    expect(extractApiError({ detail: 'Repo not found' }, FALLBACK)).toBe(
      'Repo not found',
    );
  });

  it('prefers message over error for an object detail', () => {
    expect(
      extractApiError({ detail: { error: 'x', message: 'msg' } }, FALLBACK),
    ).toBe('msg');
  });

  it('falls back to the first string value when no message/error key', () => {
    expect(extractApiError({ detail: { reason: 'r' } }, FALLBACK)).toBe('r');
  });

  it('renders a pydantic-422 list as "<last loc>: <msg>"', () => {
    expect(
      extractApiError(
        { detail: [{ loc: ['body', 'name'], msg: 'field required' }] },
        FALLBACK,
      ),
    ).toBe('name: field required');
  });

  it('joins multiple pydantic-422 items with "; "', () => {
    expect(
      extractApiError(
        {
          detail: [
            { loc: ['body', 'name'], msg: 'field required' },
            { loc: ['body', 'age'], msg: 'must be > 0' },
          ],
        },
        FALLBACK,
      ),
    ).toBe('name: field required; age: must be > 0');
  });

  it('uses fallback for an empty object', () => {
    expect(extractApiError({}, FALLBACK)).toBe(FALLBACK);
  });

  it('uses fallback for null', () => {
    expect(extractApiError(null, FALLBACK)).toBe(FALLBACK);
  });

  it('reads a top-level error string when detail is absent', () => {
    expect(extractApiError({ error: 'boom' }, FALLBACK)).toBe('boom');
  });

  it('reads a top-level message string when detail and error are absent', () => {
    expect(extractApiError({ message: 'note' }, FALLBACK)).toBe('note');
  });

  it('returns the real MFA freshness message (object detail)', () => {
    expect(
      extractApiError(
        {
          detail: {
            error: 'mfa_freshness_required',
            message: 'Fresh MFA required — re-verify',
          },
        },
        FALLBACK,
      ),
    ).toBe('Fresh MFA required — re-verify');
  });

  it('falls through to fallback when detail object has no string value', () => {
    expect(
      extractApiError({ detail: { code: 42, nested: { a: 1 } } }, FALLBACK),
    ).toBe(FALLBACK);
  });

  it('falls through when a 422 list has no usable msg', () => {
    expect(
      extractApiError({ detail: [{ loc: ['body'] }] }, FALLBACK),
    ).toBe(FALLBACK);
  });
});

describe('extractApiError — never leaks "[object Object]"', () => {
  const inputs: unknown[] = [
    { detail: 'str' },
    { detail: { error: 'x', message: 'msg' } },
    { detail: { reason: 'r' } },
    { detail: [{ loc: ['body', 'name'], msg: 'field required' }] },
    { detail: { code: 42, nested: { a: 1 } } }, // no string → fallback
    { detail: [{ loc: ['body'] }] }, // unusable list → fallback
    { detail: { error: 'mfa_freshness_required', message: 'Fresh MFA required' } },
    {},
    null,
    undefined,
    'a-bare-string',
    42,
    { detail: 12345 },
  ];

  it.each(inputs.map((v) => [JSON.stringify(v) ?? String(v), v]))(
    'input %s never produces [object Object]',
    (_label, value) => {
      const out = extractApiError(value, FALLBACK);
      expect(typeof out).toBe('string');
      expect(out).not.toContain('[object Object]');
    },
  );
});
