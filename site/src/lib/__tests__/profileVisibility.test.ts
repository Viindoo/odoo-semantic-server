// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the admin profile visibility badges.
 *
 * Business rules under protection:
 *   - A profile with no tenant reads as shared with every key; a tenant-owned
 *     one names its tenant (or its id when the name is unknown to the viewer).
 *   - A SHARED profile whose ancestor chain holds a private profile is flagged,
 *     naming the nearest private ancestor - even through a shared middle link.
 *   - A private profile, or a shared one under shared ancestors, is not flagged.
 *   - A corrupt parent cycle terminates (no infinite loop).
 */
import { describe, expect, it } from 'vitest';

import { privateAncestorOfShared, visibilityLabel } from '../profileVisibility';

const odoo20 = { id: 1, name: 'odoo_20', tenant_id: null, parent_profile_id: null };
const std19 = { id: 2, name: 'standard_viindoo_19', tenant_id: 67, parent_profile_id: null };
const internal19 = { id: 3, name: 'viindoo_internal_19', tenant_id: null, parent_profile_id: 2 };
const sharedMid = { id: 4, name: 'mid', tenant_id: null, parent_profile_id: 2 };
const sharedLeaf = { id: 5, name: 'leaf', tenant_id: null, parent_profile_id: 4 };
const std20 = { id: 6, name: 'standard_viindoo_20', tenant_id: 67, parent_profile_id: 1 };

describe('visibilityLabel', () => {
  it('reads a tenant-less profile as shared with every key', () => {
    expect(visibilityLabel(odoo20)).toBe('Shared (all keys)');
  });

  it('names the owning tenant, or its id when the name is unknown', () => {
    expect(visibilityLabel(std19, { 67: 'Viindoo Technology JSC' }))
      .toBe('Private: Viindoo Technology JSC');
    expect(visibilityLabel(std19)).toBe('Private: tenant 67');
  });
});

describe('privateAncestorOfShared', () => {
  const all = [odoo20, std19, internal19, sharedMid, sharedLeaf, std20];

  it('flags a shared child of a private parent', () => {
    expect(privateAncestorOfShared(internal19, all)?.name).toBe('standard_viindoo_19');
  });

  it('flags a shared profile whose private ancestor sits above a shared middle link', () => {
    expect(privateAncestorOfShared(sharedLeaf, all)?.name).toBe('standard_viindoo_19');
  });

  it('does not flag a private profile, nor a shared one under shared ancestors', () => {
    expect(privateAncestorOfShared(std20, all)).toBeNull();
    expect(privateAncestorOfShared(odoo20, all)).toBeNull();
  });

  it('terminates on a corrupt parent cycle', () => {
    const a = { id: 10, name: 'a', tenant_id: null, parent_profile_id: 11 };
    const b = { id: 11, name: 'b', tenant_id: null, parent_profile_id: 10 };
    expect(privateAncestorOfShared(a, [a, b])).toBeNull();
  });
});
