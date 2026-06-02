// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the ASTRO_DEV_ORIGIN → allowedDomains env-gate logic in astro.config.mjs.
 *
 * Issue #236 root-cause (Astro 6.3.3):
 *   When `security.allowedDomains` is empty, `validateHost()` in the node adapter
 *   returns `undefined` and the request URL origin falls back to "http://localhost:4321".
 *   A browser fetch from http://127.0.0.1:4321 sends Origin: http://127.0.0.1:4321,
 *   which does NOT match "http://localhost:4321".  Multipart/form-data requests
 *   (including the restore-upload endpoint) are "form-like" in Astro's checkOrigin
 *   heuristic, so they receive a 403 even though the request is genuinely same-origin.
 *
 * Fix:
 *   `pnpm dev` re-evaluates astro.config.mjs at startup with ASTRO_DEV_ORIGIN set, so
 *   allowedDomains is populated and 127.0.0.1 is accepted.
 *   For preview, allowedDomains must be baked in at build time: use `pnpm build:dev`
 *   (or the `pnpm preview:dev` alias) instead of a plain `pnpm build`.
 *   checkOrigin stays enabled everywhere.
 *
 * SSOT: `parseDevOrigin` and `wouldCheckOriginBlock` live in
 * `../check-origin-config.mjs` and are imported here directly — this test exercises
 * the real config logic, not a hand-copied duplicate.
 */

import { describe, expect, it } from 'vitest';
import { parseDevOrigin, wouldCheckOriginBlock } from '../check-origin-config.mjs';

describe('parseDevOrigin (ASTRO_DEV_ORIGIN env gate)', () => {
  it('returns empty array when env is undefined (prod build, checkOrigin uses localhost fallback)', () => {
    expect(parseDevOrigin(undefined)).toEqual([]);
  });

  it('returns empty array when env is empty string', () => {
    expect(parseDevOrigin('')).toEqual([]);
  });

  it('parses http://127.0.0.1:4321 correctly', () => {
    const result = parseDevOrigin('http://127.0.0.1:4321');
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ hostname: '127.0.0.1', port: '4321', protocol: 'http' });
  });

  it('parses http://localhost:4321 correctly', () => {
    const result = parseDevOrigin('http://localhost:4321');
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ hostname: 'localhost', port: '4321', protocol: 'http' });
  });

  it('parses https://dev.example.com correctly (no explicit port)', () => {
    const result = parseDevOrigin('https://dev.example.com');
    expect(result).toHaveLength(1);
    // URL.port is '' for default ports → becomes undefined
    expect(result[0]).toEqual({ hostname: 'dev.example.com', port: undefined, protocol: 'https' });
  });

  it('returns empty array for a malformed URL (graceful fallback)', () => {
    expect(parseDevOrigin('not-a-url')).toEqual([]);
  });

  it('returns empty array for a bare hostname without scheme (not a valid URL)', () => {
    expect(parseDevOrigin('127.0.0.1:4321')).toEqual([]);
  });
});

describe('Astro 6.3.3 checkOrigin — isSameOrigin heuristic', () => {
  /**
   * Executable documentation of the Astro middleware logic from
   * node_modules/astro/dist/core/app/middlewares.js.
   * Guards against upstream changes that would reintroduce #236.
   */

  it('multipart/form-data same-origin should NOT be blocked', () => {
    // After fix: url.origin = "http://127.0.0.1:4321" (allowedDomains set correctly)
    expect(wouldCheckOriginBlock(
      'POST',
      'multipart/form-data; boundary=abc',
      'http://127.0.0.1:4321',
      'http://127.0.0.1:4321',   // url.origin WITH fix applied
    )).toBe(false);
  });

  it('multipart/form-data with localhost fallback origin DOES block 127.0.0.1 Origin (root cause of #236)', () => {
    // Without fix: url.origin falls back to "http://localhost:4321"
    expect(wouldCheckOriginBlock(
      'POST',
      'multipart/form-data; boundary=abc',
      'http://127.0.0.1:4321',   // Origin browser sends
      'http://localhost:4321',   // url.origin WITHOUT fix (localhost fallback)
    )).toBe(true);
  });

  it('multipart/form-data cross-origin should be blocked', () => {
    expect(wouldCheckOriginBlock(
      'POST',
      'multipart/form-data; boundary=abc',
      'http://evil.example.com',
      'http://127.0.0.1:4321',
    )).toBe(true);
  });

  it('application/json is never blocked (not form-like)', () => {
    expect(wouldCheckOriginBlock(
      'POST',
      'application/json',
      'http://127.0.0.1:4321',
      'http://127.0.0.1:4321',
    )).toBe(false);

    // Even cross-origin JSON passes the formLikeHeader branch
    expect(wouldCheckOriginBlock(
      'POST',
      'application/json',
      'http://evil.example.com',
      'http://127.0.0.1:4321',
    )).toBe(false);
  });

  it('no Origin header + no content-type blocks non-same-origin', () => {
    expect(wouldCheckOriginBlock('POST', null, null, 'http://127.0.0.1:4321')).toBe(true);
  });

  it('GET/HEAD/OPTIONS are never blocked', () => {
    for (const method of ['GET', 'HEAD', 'OPTIONS']) {
      expect(wouldCheckOriginBlock(method, 'multipart/form-data', null, 'http://127.0.0.1:4321')).toBe(false);
    }
  });
});
