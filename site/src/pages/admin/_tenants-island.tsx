// SPDX-License-Identifier: AGPL-3.0-or-later
// Tenants React island — admin-only mutations for W1 (ADR-0038)
// Handles: create/edit/deactivate/delete tenant, add/remove members,
// assign profiles and repos to tenants.
import { useState, useEffect } from 'react';
import { submitJson } from '../../lib/apiClient';
import { flash } from '../../lib/flash';

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

type RepoItem = {
  id: number;
  url: string;
  branch: string;
  profile_name: string | null;
  tenant_id: number | null;
};

type ProfileItem = {
  id: number;
  name: string;
  odoo_version: string;
  tenant_id: number | null;
};

interface Props {
  initialTenants: Tenant[];
  allUsers: User[];
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

  // Repo assignment state
  const [assignedRepos, setAssignedRepos] = useState<RepoItem[]>([]);
  const [unassignedRepos, setUnassignedRepos] = useState<RepoItem[]>([]);
  const [assignRepoId, setAssignRepoId] = useState('');
  const [loadingRepos, setLoadingRepos] = useState(false);

  // Profile assignment state
  const [assignedProfiles, setAssignedProfiles] = useState<ProfileItem[]>([]);
  const [unassignedProfiles, setUnassignedProfiles] = useState<ProfileItem[]>([]);
  const [assignProfileId, setAssignProfileId] = useState('');
  const [loadingProfiles, setLoadingProfiles] = useState(false);

  async function refreshTenants() {
    const r = await submitJson<{ tenants: Tenant[] }>('/api/tenants', { method: 'GET', stepUp: false });
    if (r.ok) setTenants(r.data.tenants ?? []);
  }

  async function loadMembers(tenantId: number) {
    setLoadingMembers(true);
    const r = await submitJson<{ members: Member[] }>(`/api/tenants/${tenantId}/members`, { method: 'GET', stepUp: false });
    if (r.ok) setMembers(r.data.members ?? []);
    setLoadingMembers(false);
  }

  async function loadRepoAssignments(tenantId: number) {
    setLoadingRepos(true);
    const r = await submitJson<{ profiles: { tenant_id: number | null; repos: RepoItem[] }[] }>('/api/repos/profiles', { method: 'GET', stepUp: false });
    if (r.ok) {
      const profiles = r.data.profiles ?? [];
      const allRepos: RepoItem[] = profiles.flatMap(p =>
        (p.repos ?? []).map(rr => ({ ...rr, tenant_id: rr.tenant_id ?? null }))
      );
      setAssignedRepos(allRepos.filter(rr => rr.tenant_id === tenantId));
      setUnassignedRepos(allRepos.filter(rr => rr.tenant_id === null));
    }
    setLoadingRepos(false);
  }

  async function loadProfileAssignments(tenantId: number) {
    setLoadingProfiles(true);
    const r = await submitJson<{ profiles: ProfileItem[] }>('/api/repos/profiles', { method: 'GET', stepUp: false });
    if (r.ok) {
      const allProfiles: ProfileItem[] = (r.data.profiles ?? []).map(p => ({
        id: p.id, name: p.name, odoo_version: p.odoo_version, tenant_id: p.tenant_id ?? null,
      }));
      setAssignedProfiles(allProfiles.filter(p => p.tenant_id === tenantId));
      setUnassignedProfiles(allProfiles.filter(p => p.tenant_id === null));
    }
    setLoadingProfiles(false);
  }

  async function handleAssignRepo() {
    if (!selectedTenant || !assignRepoId) return;
    const r = await submitJson(`/api/repos/${assignRepoId}/tenant`, {
      method: 'PATCH',
      body: { tenant_id: selectedTenant.id },
    });
    if (r.ok) {
      flash('Repo assigned');
      setAssignRepoId('');
      await loadRepoAssignments(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleUnassignRepo(repoId: number) {
    if (!selectedTenant) return;
    const r = await submitJson(`/api/repos/${repoId}/tenant`, {
      method: 'PATCH',
      body: { tenant_id: null },
    });
    if (r.ok) {
      flash('Repo unassigned');
      await loadRepoAssignments(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleAssignProfile() {
    if (!selectedTenant || !assignProfileId) return;
    const r = await submitJson(`/api/profiles/${assignProfileId}/tenant`, {
      method: 'PATCH',
      body: { tenant_id: selectedTenant.id },
    });
    if (r.ok) {
      flash('Profile assigned');
      setAssignProfileId('');
      await loadProfileAssignments(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleUnassignProfile(profileId: number) {
    if (!selectedTenant) return;
    const r = await submitJson(`/api/profiles/${profileId}/tenant`, {
      method: 'PATCH',
      body: { tenant_id: null },
    });
    if (r.ok) {
      flash('Profile unassigned');
      await loadProfileAssignments(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  useEffect(() => {
    if (selectedTenant) {
      loadMembers(selectedTenant.id);
      loadRepoAssignments(selectedTenant.id);
      loadProfileAssignments(selectedTenant.id);
    }
  }, [selectedTenant?.id]);

  async function handleCreateTenant(e: React.FormEvent) {
    e.preventDefault();
    if (!newTenantName.trim()) return;
    setCreating(true);
    const r = await submitJson('/api/tenants', {
      method: 'POST',
      body: { name: newTenantName.trim() },
    });
    setCreating(false);
    if (r.ok) {
      flash('Tenant created');
      setNewTenantName('');
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleUpdateTenant(tenantId: number) {
    if (!editName.trim()) return;
    const r = await submitJson(`/api/tenants/${tenantId}`, {
      method: 'PATCH',
      body: { name: editName.trim() },
    });
    if (r.ok) {
      flash('Tenant updated');
      setEditingId(null);
      await refreshTenants();
      if (selectedTenant?.id === tenantId) {
        const updated = tenants.find(t => t.id === tenantId);
        if (updated) setSelectedTenant({ ...updated, name: editName.trim() });
      }
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleToggleActive(tenant: Tenant) {
    const r = await submitJson(`/api/tenants/${tenant.id}`, {
      method: 'PATCH',
      body: { active: !tenant.active },
    });
    if (r.ok) {
      flash(`Tenant ${tenant.active ? 'deactivated' : 'activated'}`);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleDeleteTenant(tenant: Tenant) {
    if (!confirm(`Delete tenant "${tenant.name}"? This cannot be undone.`)) return;
    const r = await submitJson(`/api/tenants/${tenant.id}`, { method: 'DELETE' });
    if (r.ok) {
      flash('Tenant deleted');
      if (selectedTenant?.id === tenant.id) setSelectedTenant(null);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleAddMember(e: React.FormEvent) {
    e.preventDefault();
    if (!selectedTenant || !addMemberUserId) return;
    const r = await submitJson(`/api/tenants/${selectedTenant.id}/members`, {
      method: 'POST',
      body: { user_id: Number(addMemberUserId), role: addMemberRole },
    });
    if (r.ok) {
      flash('Member added');
      setAddMemberUserId('');
      await loadMembers(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
    }
  }

  async function handleRemoveMember(userId: number) {
    if (!selectedTenant) return;
    const r = await submitJson(`/api/tenants/${selectedTenant.id}/members/${userId}`, {
      method: 'DELETE',
    });
    if (r.ok) {
      flash('Member removed');
      await loadMembers(selectedTenant.id);
      await refreshTenants();
    } else {
      flash(r.error!, { error: true });
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

          {/* Repo assignment widget */}
          <div className="border-t border-gray-100 pt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">
              Repos assigned to this tenant ({assignedRepos.length})
            </h3>
            {loadingRepos ? (
              <p className="text-xs text-gray-400">Loading...</p>
            ) : assignedRepos.length === 0 ? (
              <p className="text-xs text-gray-400 mb-2">No repos assigned yet.</p>
            ) : (
              <table className="w-full text-sm mb-3">
                <thead className="text-xs text-gray-500 bg-gray-50">
                  <tr>
                    <th className="px-3 py-1 text-left">URL</th>
                    <th className="px-3 py-1 text-left">Branch</th>
                    <th className="px-3 py-1 text-left">Profile</th>
                    <th className="px-3 py-1 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {assignedRepos.map((r) => (
                    <tr key={r.id} className="border-t border-gray-100">
                      <td className="px-3 py-1 font-mono text-xs text-gray-700 max-w-[220px] truncate" title={r.url}>{r.url}</td>
                      <td className="px-3 py-1 text-gray-500 text-xs">{r.branch}</td>
                      <td className="px-3 py-1 text-gray-500 text-xs">{r.profile_name ?? '-'}</td>
                      <td className="px-3 py-1 text-right">
                        <button
                          onClick={() => handleUnassignRepo(r.id)}
                          className="text-xs text-red-600 hover:underline"
                        >Unassign</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {unassignedRepos.length > 0 && (
              <div className="flex gap-2 items-end mt-1">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Assign repo</label>
                  <select
                    value={assignRepoId}
                    onChange={(e) => setAssignRepoId((e.target as HTMLSelectElement).value)}
                    className="border border-gray-300 rounded-lg px-2 py-1 text-sm"
                  >
                    <option value="">-- select unassigned repo --</option>
                    {unassignedRepos.map(r => (
                      <option key={r.id} value={String(r.id)}>
                        {r.url} [{r.branch}]{r.profile_name ? ` (${r.profile_name})` : ''}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  onClick={handleAssignRepo}
                  disabled={!assignRepoId}
                  className="px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50"
                >Assign</button>
              </div>
            )}
            {unassignedRepos.length === 0 && !loadingRepos && (
              <p className="text-xs text-gray-400 mt-1">No unassigned repos available.</p>
            )}
          </div>

          {/* Profile assignment widget */}
          <div className="border-t border-gray-100 pt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">
              Profiles assigned to this tenant ({assignedProfiles.length})
            </h3>
            {loadingProfiles ? (
              <p className="text-xs text-gray-400">Loading...</p>
            ) : assignedProfiles.length === 0 ? (
              <p className="text-xs text-gray-400 mb-2">No profiles assigned yet.</p>
            ) : (
              <table className="w-full text-sm mb-3">
                <thead className="text-xs text-gray-500 bg-gray-50">
                  <tr>
                    <th className="px-3 py-1 text-left">Name</th>
                    <th className="px-3 py-1 text-left">Version</th>
                    <th className="px-3 py-1 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {assignedProfiles.map((p) => (
                    <tr key={p.id} className="border-t border-gray-100">
                      <td className="px-3 py-1 font-medium text-gray-700">{p.name}</td>
                      <td className="px-3 py-1 text-gray-500 text-xs">{p.odoo_version}</td>
                      <td className="px-3 py-1 text-right">
                        <button
                          onClick={() => handleUnassignProfile(p.id)}
                          className="text-xs text-red-600 hover:underline"
                        >Unassign</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {unassignedProfiles.length > 0 && (
              <div className="flex gap-2 items-end mt-1">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Assign profile</label>
                  <select
                    value={assignProfileId}
                    onChange={(e) => setAssignProfileId((e.target as HTMLSelectElement).value)}
                    className="border border-gray-300 rounded-lg px-2 py-1 text-sm"
                  >
                    <option value="">-- select unassigned profile --</option>
                    {unassignedProfiles.map(p => (
                      <option key={p.id} value={String(p.id)}>
                        {p.name} ({p.odoo_version})
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  onClick={handleAssignProfile}
                  disabled={!assignProfileId}
                  className="px-3 py-1.5 bg-blue-600 text-white text-xs rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50"
                >Assign</button>
              </div>
            )}
            {unassignedProfiles.length === 0 && !loadingProfiles && (
              <p className="text-xs text-gray-400 mt-1">No unassigned profiles available.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
