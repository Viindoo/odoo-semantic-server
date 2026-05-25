// SPDX-License-Identifier: AGPL-3.0-or-later
// Tenants React island — admin-only mutations for W1 (ADR-0038)
// Handles: create/edit/deactivate/delete tenant, add/remove members,
// assign profiles and repos to tenants.
import { useState, useEffect } from 'react';

type Tenant = {
  id: number;
  name: string;
  active: boolean;
  created_at: string | null;
  member_count: number;
  repo_count: number;
  profile_count: number;
};

type User = {
  id: number;
  username: string;
  email: string | null;
};

type Member = {
  user_id: number;
  username: string;
  email: string | null;
  role: string;
  created_at: string | null;
};

interface Props {
  initialTenants: Tenant[];
  allUsers: User[];
}

function flash(msg: string, isError = false) {
  const el = document.querySelector('[data-testid="flash-banner"]') as HTMLElement | null;
  if (!el) return;
  el.textContent = msg;
  el.className = `fixed top-4 right-4 z-50 px-5 py-3 rounded-xl shadow-lg text-sm font-medium border ${
    isError ? 'bg-red-50 border-red-300 text-red-800' : 'bg-green-50 border-green-300 text-green-800'
  }`;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 4000);
}

async function apiFetch(url: string, opts: RequestInit = {}): Promise<{ ok: boolean; data: unknown }> {
  try {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(opts.headers ?? {}) },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, data };
  } catch (e) {
    return { ok: false, data: { error: String(e) } };
  }
}

export default function TenantsIsland({ initialTenants, allUsers }: Props) {
  const [tenants, setTenants] = useState<Tenant[]>(initialTenants);
  const [selectedTenant, setSelectedTenant] = useState<Tenant | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [loadingMembers, setLoadingMembers] = useState(false);

  // Create tenant state
  const [newTenantName, setNewTenantName] = useState('');
  const [creating, setCreating] = useState(false);

  // Edit tenant state
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState('');

  // Add member state
  const [addMemberUserId, setAddMemberUserId] = useState('');
  const [addMemberRole, setAddMemberRole] = useState('member');

  async function refreshTenants() {
    const { ok, data } = await apiFetch('/api/tenants');
    if (ok) setTenants((data as { tenants: Tenant[] }).tenants ?? []);
  }

  async function loadMembers(tenantId: number) {
    setLoadingMembers(true);
    const { ok, data } = await apiFetch(`/api/tenants/${tenantId}/members`);
    if (ok) setMembers((data as { members: Member[] }).members ?? []);
    setLoadingMembers(false);
  }

  useEffect(() => {
    if (selectedTenant) loadMembers(selectedTenant.id);
  }, [selectedTenant?.id]);

  async function handleCreateTenant(e: React.FormEvent) {
    e.preventDefault();
    if (!newTenantName.trim()) return;
    setCreating(true);
    const { ok, data } = await apiFetch('/api/tenants', {
      method: 'POST',
      body: JSON.stringify({ name: newTenantName.trim() }),
    });
    setCreating(false);
    if (ok) {
      flash('Tenant created');
      setNewTenantName('');
      await refreshTenants();
    } else {
      flash((data as { error?: string }).error ?? 'Failed to create tenant', true);
    }
  }

  async function handleUpdateTenant(tenantId: number) {
    if (!editName.trim()) return;
    const { ok, data } = await apiFetch(`/api/tenants/${tenantId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: editName.trim() }),
    });
    if (ok) {
      flash('Tenant updated');
      setEditingId(null);
      await refreshTenants();
      if (selectedTenant?.id === tenantId) {
        const updated = tenants.find(t => t.id === tenantId);
        if (updated) setSelectedTenant({ ...updated, name: editName.trim() });
      }
    } else {
      flash((data as { error?: string }).error ?? 'Failed to update tenant', true);
    }
  }

  async function handleToggleActive(tenant: Tenant) {
    const { ok, data } = await apiFetch(`/api/tenants/${tenant.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ active: !tenant.active }),
    });
    if (ok) {
      flash(`Tenant ${tenant.active ? 'deactivated' : 'activated'}`);
      await refreshTenants();
    } else {
      flash((data as { error?: string }).error ?? 'Failed to update tenant', true);
    }
  }

  async function handleDeleteTenant(tenant: Tenant) {
    if (!confirm(`Delete tenant "${tenant.name}"? This cannot be undone.`)) return;
    const { ok, data } = await apiFetch(`/api/tenants/${tenant.id}`, { method: 'DELETE' });
    if (ok) {
      flash('Tenant deleted');
      if (selectedTenant?.id === tenant.id) setSelectedTenant(null);
      await refreshTenants();
    } else {
      flash((data as { error?: string }).error ?? 'Failed to delete tenant', true);
    }
  }

  async function handleAddMember(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedTenant || !addMemberUserId) return;
    const { ok, data } = await apiFetch(`/api/tenants/${selectedTenant.id}/members`, {
      method: 'POST',
      body: JSON.stringify({ user_id: Number(addMemberUserId), role: addMemberRole }),
    });
    if (ok) {
      flash('Member added');
      setAddMemberUserId('');
      await loadMembers(selectedTenant.id);
      await refreshTenants();
    } else {
      flash((data as { error?: string }).error ?? 'Failed to add member', true);
    }
  }

  async function handleRemoveMember(userId: number) {
    if (!selectedTenant) return;
    const { ok, data } = await apiFetch(`/api/tenants/${selectedTenant.id}/members/${userId}`, {
      method: 'DELETE',
    });
    if (ok) {
      flash('Member removed');
      await loadMembers(selectedTenant.id);
      await refreshTenants();
    } else {
      flash((data as { error?: string }).error ?? 'Failed to remove member', true);
    }
  }

  const availableUsers = allUsers.filter(u => !members.some(m => m.user_id === u.id));

  return (
    <div className="space-y-6">
      {/* Create tenant form */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <h2 className="text-base font-semibold text-gray-800 mb-3">Create New Tenant</h2>
        <form onSubmit={handleCreateTenant} className="flex gap-3 items-end">
          <div className="flex-1">
            <label className="block text-xs font-medium text-gray-600 mb-1">Tenant Name</label>
            <input
              type="text"
              value={newTenantName}
              onInput={(e) => setNewTenantName((e.target as HTMLInputElement).value)}
              placeholder="e.g. Acme Corp"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"
              required
            />
          </div>
          <button
            type="submit"
            disabled={creating || !newTenantName.trim()}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {creating ? 'Creating...' : '+ Create Tenant'}
          </button>
        </form>
      </div>

      {/* Tenant list */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100">
          <h2 className="text-base font-semibold text-gray-800">All Tenants</h2>
        </div>
        {tenants.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-gray-400">No tenants yet. Create one above.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
              <tr>
                <th className="px-4 py-2 text-left">Name</th>
                <th className="px-4 py-2 text-center">Status</th>
                <th className="px-4 py-2 text-center">Members</th>
                <th className="px-4 py-2 text-center">Repos</th>
                <th className="px-4 py-2 text-center">Profiles</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((t) => (
                <tr
                  key={t.id}
                  className={`border-t border-gray-100 cursor-pointer hover:bg-blue-50 ${selectedTenant?.id === t.id ? 'bg-blue-50' : ''}`}
                  onClick={() => setSelectedTenant(selectedTenant?.id === t.id ? null : t)}
                >
                  <td className="px-4 py-2 font-medium text-gray-900">
                    {editingId === t.id ? (
                      <span className="flex gap-2 items-center" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="text"
                          value={editName}
                          onInput={(e) => setEditName((e.target as HTMLInputElement).value)}
                          className="border border-gray-300 rounded px-2 py-1 text-sm w-40"
                          autoFocus
                        />
                        <button
                          onClick={() => handleUpdateTenant(t.id)}
                          className="text-green-600 text-xs font-medium hover:underline"
                        >Save</button>
                        <button
                          onClick={() => setEditingId(null)}
                          className="text-gray-400 text-xs hover:underline"
                        >Cancel</button>
                      </span>
                    ) : t.name}
                  </td>
                  <td className="px-4 py-2 text-center">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${t.active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                      {t.active ? 'Active' : 'Inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-center text-gray-600">{t.member_count}</td>
                  <td className="px-4 py-2 text-center text-gray-600">{t.repo_count}</td>
                  <td className="px-4 py-2 text-center text-gray-600">{t.profile_count}</td>
                  <td className="px-4 py-2 text-right space-x-2" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => { setEditingId(t.id); setEditName(t.name); }}
                      className="text-xs text-blue-600 hover:underline"
                    >Edit</button>
                    <button
                      onClick={() => handleToggleActive(t)}
                      className="text-xs text-amber-600 hover:underline"
                    >{t.active ? 'Deactivate' : 'Activate'}</button>
                    <button
                      onClick={() => handleDeleteTenant(t)}
                      className="text-xs text-red-600 hover:underline"
                    >Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Tenant detail panel */}
      {selectedTenant && (
        <div className="bg-white rounded-xl border border-blue-200 p-4 space-y-5">
          <h2 className="text-base font-semibold text-gray-800">
            Detail: <span className="text-blue-700">{selectedTenant.name}</span>
          </h2>

          {/* Members section */}
          <div>
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Members</h3>
            {loadingMembers ? (
              <p className="text-xs text-gray-400">Loading...</p>
            ) : members.length === 0 ? (
              <p className="text-xs text-gray-400">No members. Add one below.</p>
            ) : (
              <table className="w-full text-sm mb-3">
                <thead className="text-xs text-gray-500 bg-gray-50">
                  <tr>
                    <th className="px-3 py-1 text-left">Username</th>
                    <th className="px-3 py-1 text-left">Email</th>
                    <th className="px-3 py-1 text-center">Role</th>
                    <th className="px-3 py-1 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr key={m.user_id} className="border-t border-gray-100">
                      <td className="px-3 py-1 font-medium">{m.username}</td>
                      <td className="px-3 py-1 text-gray-500">{m.email ?? '-'}</td>
                      <td className="px-3 py-1 text-center">
                        <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{m.role}</span>
                      </td>
                      <td className="px-3 py-1 text-right">
                        <button
                          onClick={() => handleRemoveMember(m.user_id)}
                          className="text-xs text-red-600 hover:underline"
                        >Remove</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {/* Add member form */}
            {availableUsers.length > 0 && (
              <form onSubmit={handleAddMember} className="flex gap-2 items-end mt-2">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">User</label>
                  <select
                    value={addMemberUserId}
                    onChange={(e) => setAddMemberUserId((e.target as HTMLSelectElement).value)}
                    className="border border-gray-300 rounded-lg px-2 py-1 text-sm"
                    required
                  >
                    <option value="">-- select user --</option>
                    {availableUsers.map(u => (
                      <option key={u.id} value={String(u.id)}>{u.username}{u.email ? ` (${u.email})` : ''}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Role</label>
                  <select
                    value={addMemberRole}
                    onChange={(e) => setAddMemberRole((e.target as HTMLSelectElement).value)}
                    className="border border-gray-300 rounded-lg px-2 py-1 text-sm"
                  >
                    <option value="member">member</option>
                    <option value="tenant_admin">tenant_admin</option>
                  </select>
                </div>
                <button
                  type="submit"
                  disabled={!addMemberUserId}
                  className="px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50"
                >Add Member</button>
              </form>
            )}
            {availableUsers.length === 0 && members.length > 0 && (
              <p className="text-xs text-gray-400 mt-2">All available users are already members.</p>
            )}
          </div>

          {/* Info about assigned resources */}
          <div className="text-xs text-gray-500 border-t border-gray-100 pt-3">
            <p>
              This tenant has <strong>{selectedTenant.repo_count}</strong> repo(s) and{' '}
              <strong>{selectedTenant.profile_count}</strong> profile(s) assigned.
              Use <code>PATCH /api/repos/&#123;id&#125;/tenant</code> or{' '}
              <code>PATCH /api/profiles/&#123;id&#125;/tenant</code> to assign/unassign resources.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
