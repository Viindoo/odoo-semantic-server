// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Drift-guard for the tools/resources SSOT (`lib/tools-data.ts`) against the
 * count constants (`lib/constants.ts`).
 *
 * Business rule under protection: the public site advertises exactly
 * TOOL_COUNT (24) MCP tools and RESOURCE_COUNT (7) odoo:// resources. The
 * homepage, /tools page, and /pricing all render from these arrays + counts.
 * If someone adds or removes a tool/resource entry without bumping the
 * constant (or vice versa), the advertised number desyncs from what is shown.
 * These tests fail precisely when that contract is violated.
 */

import { describe, expect, it } from 'vitest';
import { RESOURCES, TOOLS } from '../tools-data';
import { RESOURCE_COUNT, TOOL_COUNT } from '../constants';

describe('tools-data SSOT vs count constants', () => {
  it('TOOLS array length equals TOOL_COUNT', () => {
    expect(TOOLS.length).toBe(TOOL_COUNT);
  });

  it('RESOURCES array length equals RESOURCE_COUNT', () => {
    expect(RESOURCES.length).toBe(RESOURCE_COUNT);
  });

  it('tool num fields are unique and zero-padded sequential 01..N', () => {
    const nums = TOOLS.map((t) => t.num);
    expect(new Set(nums).size).toBe(nums.length); // unique
    const expected = Array.from({ length: TOOL_COUNT }, (_, i) =>
      String(i + 1).padStart(2, '0'),
    );
    expect(nums).toEqual(expected);
  });

  it('every tool name is non-empty and unique', () => {
    const names = TOOLS.map((t) => t.name);
    expect(names.every((n) => n.length > 0)).toBe(true);
    expect(new Set(names).size).toBe(names.length);
  });

  it('every resource uri is non-empty and unique', () => {
    const uris = RESOURCES.map((r) => r.uri);
    expect(uris.every((u) => u.length > 0)).toBe(true);
    expect(new Set(uris).size).toBe(uris.length);
  });
});
