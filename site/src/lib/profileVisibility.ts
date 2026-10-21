// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Profile visibility helpers for the admin repos page (flat list + tree view).
 *
 * The server enforces the rule (RepoStore.check_child_visibility): a profile is
 * never more visible than its parent. These helpers only DISPLAY it, so an admin
 * can see at a glance which profiles every key can read and spot rows created
 * before the rule existed.
 *
 *   tenant_id null  -> shared: every API key (free tier included) can read it.
 *   tenant_id N     -> private to tenant N.
 */

export interface VisibilityProfile {
  id: number;
  name: string;
  tenant_id?: number | null;
  parent_profile_id?: number | null;
}

export type TenantNames = Record<number, string>;

/** "Shared (all keys)" or "Private: <tenant name>" (falls back to the tenant id). */
export function visibilityLabel(p: VisibilityProfile, tenantNames: TenantNames = {}): string {
  if (p.tenant_id === null || p.tenant_id === undefined) return 'Shared (all keys)';
  return `Private: ${tenantNames[p.tenant_id] ?? `tenant ${p.tenant_id}`}`;
}

/**
 * The nearest private ancestor of a SHARED profile, or null.
 *
 * A shared profile under a private ancestor publishes that private chain's
 * names and repos to every key - the shape the visibility rule forbids. Walks
 * parent links with a visited set (a corrupt cycle cannot loop forever); an
 * ancestor missing from the list (not visible to this viewer) stops the walk.
 */
export function privateAncestorOfShared(
  p: VisibilityProfile,
  all: VisibilityProfile[],
): VisibilityProfile | null {
  if (p.tenant_id !== null && p.tenant_id !== undefined) return null;
  const byId = new Map(all.map((x) => [x.id, x]));
  const seen = new Set<number>([p.id]);
  let parentId = p.parent_profile_id ?? null;
  while (parentId !== null && !seen.has(parentId)) {
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent) return null;
    if (parent.tenant_id !== null && parent.tenant_id !== undefined) return parent;
    parentId = parent.parent_profile_id ?? null;
  }
  return null;
}
