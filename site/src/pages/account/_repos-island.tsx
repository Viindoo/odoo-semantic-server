// SPDX-License-Identifier: AGPL-3.0-or-later
// My Repositories React island — customer self-service portal (W2, ADR-0038)
// Handles: add repo, trigger index, delete repo — all tenant-scoped.
// IMPORTANT: use className= and htmlFor= (NOT class= or for=) — this is a .tsx React island.
import { useState } from 'react';

type Repo = {
  id: number;
  url: string;
  branch: string;
  clone_status: string;
  tenant_id: number | null;
  status?: string;
};

type Profile = {
  id: number;
  name: string;
  odoo_version: string;
  tenant_id: number | null;
  repos: Repo[];
};

type TenantEntry = {
  tenant_id: number;
  name: string;
  role: string;
};

interface Props {
  initialProfiles: Profile[];
  initialTenants: TenantEntry[];
  isAdmin: boolean;
}

function flash(msg: string, isError = false) {
  const el = document.querySelector('[data-testid="flash-banner"]') as HTMLElement | null;
  if (!el) return;
  // Ensure live-region semantics are present so screen readers announce the message.
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.textContent = msg;
  el.className = `fixed top-4 right-4 z-50 px-5 py-3 rounded-xl shadow-lg text-sm font-medium border ${
    isError
      ? 'bg-red-50 border-red-300 text-red-800'
      : 'bg-green-50 border-green-300 text-green-800'
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

export default function ReposIsland({ initialProfiles, initialTenants, isAdmin }: Props) {
  const [profiles, setProfiles] = useState<Profile[]>(initialProfiles);
  const [tenants] = useState<TenantEntry[]>(initialTenants);

  // Add repo form state
  const [addProfile, setAddProfile] = useState('');
  const [addUrl, setAddUrl] = useState('');
  const [addBranch, setAddBranch] = useState('');
  const [adding, setAdding] = useState(false);
  const [showAddForm, setShowAddForm] = useState(false);

  // Loading state per repo action
  const [indexingId, setIndexingId] = useState<number | null>(null);
  const [deletingId, setDeletingId] = useState<number | null>(null);

  // Only show profiles that belong to one of the user's tenants (non-null tenant_id in scope)
  const tenantIds = new Set(tenants.map(t => t.tenant_id));
  const writableProfiles = profiles.filter(
    p => p.tenant_id !== null && tenantIds.has(p.tenant_id)
  );

  async function refreshProfiles() {
    const { ok, data } = await apiFetch('/api/repos/profiles');
    if (ok) {
      const d = data as { profiles?: Profile[] };
      setProfiles(d.profiles ?? []);
    }
  }

  async function handleAddRepo(e: React.FormEvent) {
    e.preventDefault();
    if (!addProfile || !addUrl || !addBranch) return;
    setAdding(true);
    const { ok, data } = await apiFetch('/api/repos/repos', {
      method: 'POST',
      body: JSON.stringify({ profile: addProfile, url: addUrl, branch: addBranch }),
    });
    setAdding(false);
    if (ok) {
      flash('Repository added successfully.');
      setAddUrl('');
      setAddBranch('');
      setShowAddForm(false);
      await refreshProfiles();
    } else {
      const err = (data as { error?: string }).error ?? 'Failed to add repository.';
      flash(err, true);
    }
  }

  async function handleIndex(repoId: number) {
    setIndexingId(repoId);
    const { ok, data } = await apiFetch(`/api/repos/repos/${repoId}/index`, {
      method: 'POST',
      body: JSON.stringify({ max_workers: '1' }),
    });
    setIndexingId(null);
    if (ok) {
      flash('Index triggered successfully.');
    } else {
      const err = (data as { error?: string }).error ?? 'Failed to trigger index.';
      flash(err, true);
    }
  }

  async function handleDelete(repoId: number, repoUrl: string) {
    if (!confirm(`Delete repository "${repoUrl}"? This cannot be undone.`)) return;
    setDeletingId(repoId);
    const { ok, data } = await apiFetch(`/api/repos/repos/${repoId}`, { method: 'DELETE' });
    setDeletingId(null);
    if (ok) {
      flash('Repository deleted.');
      await refreshProfiles();
    } else {
      const err = (data as { error?: string }).error ?? 'Failed to delete repository.';
      flash(err, true);
    }
  }

  // writable mirrors the server-side tenant_write_allowed gate: a non-admin may only
  // mutate repos in a tenant they belong to (shared/null is admin-only). Showing
  // Index/Delete on non-writable repos just produces a confusing 403 on click, so we
  // gate the actions to match what the API will actually permit.
  const allRepos = profiles.flatMap(p =>
    (p.repos ?? []).map(r => ({
      ...r,
      profileName: p.name,
      profileVersion: p.odoo_version,
      writable: isAdmin || (p.tenant_id !== null && tenantIds.has(p.tenant_id)),
    }))
  );

  return (
    <div>
      {/* Organisation badges */}
      {tenants.length > 0 && (
        <div className="mb-6 flex flex-wrap gap-2">
          {tenants.map(t => (
            <span
              key={t.tenant_id}
              data-testid="tenant-badge"
              className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-viindoo-primary/10 text-gray-700 border border-viindoo-primary/30"
            >
              🏢 {t.name}
              <span className="opacity-60">({t.role})</span>
            </span>
          ))}
        </div>
      )}

      {/* Add repo section */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold text-gray-800">Add Repository</h2>
          <button
            type="button"
            onClick={() => setShowAddForm(v => !v)}
            className="text-sm text-viindoo-primary-text hover:underline"
          >
            {showAddForm ? 'Cancel' : '+ Add'}
          </button>
        </div>

        {showAddForm && (
          writableProfiles.length === 0 ? (
            <p className="text-sm text-gray-500">
              No writable profiles found. Ask an admin to assign a profile to your organisation.
            </p>
          ) : (
            <form onSubmit={handleAddRepo} className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="add-profile" className="block text-xs font-medium text-gray-600 mb-1">
                  Profile
                </label>
                <select
                  id="add-profile"
                  value={addProfile}
                  onChange={e => setAddProfile(e.target.value)}
                  required
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                >
                  <option value="">Select profile…</option>
                  {writableProfiles.map(p => (
                    <option key={p.id} value={p.name}>
                      {p.name} ({p.odoo_version})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="add-url" className="block text-xs font-medium text-gray-600 mb-1">
                  Repository URL
                </label>
                <input
                  id="add-url"
                  type="text"
                  value={addUrl}
                  onChange={e => setAddUrl(e.target.value)}
                  placeholder="https://github.com/org/repo.git"
                  required
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
                {addUrl.trim().startsWith('git@') && (
                  <p data-testid="ssh-onboarding-hint" className="mt-1.5 text-xs text-gray-500 leading-relaxed">
                    For SSH (<code>git@…</code>) repos, the server clones using a shared,
                    admin-managed access key - you do not select a key here. Add the
                    access key's <strong>public key</strong> (published by your admin) as a
                    read-only deploy key on your git host before adding this repository.
                  </p>
                )}
              </div>
              <div>
                <label htmlFor="add-branch" className="block text-xs font-medium text-gray-600 mb-1">
                  Branch
                </label>
                <div className="flex gap-2">
                  <input
                    id="add-branch"
                    type="text"
                    value={addBranch}
                    onChange={e => setAddBranch(e.target.value)}
                    placeholder="17.0"
                    required
                    className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  />
                  <button
                    type="submit"
                    disabled={adding}
                    className="bg-viindoo-primary hover:bg-viindoo-primary-bright disabled:opacity-50 text-viindoo-bg-0 text-sm font-medium px-4 py-2 rounded-lg transition-colors"
                  >
                    {adding ? 'Adding…' : 'Add'}
                  </button>
                </div>
              </div>
            </form>
          )
        )}
      </div>

      {/* Repo list */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="text-base font-semibold text-gray-800">
            Repositories ({allRepos.length})
          </h2>
        </div>

        {allRepos.length === 0 ? (
          <div data-testid="repos-empty-state" className="py-12 text-center text-gray-500">
            <p className="text-3xl mb-2">📦</p>
            <p className="font-medium">No repositories yet.</p>
            <p className="text-sm mt-1">Add a repository to get started.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-4 py-3 text-left">Repository</th>
                  <th className="px-4 py-3 text-left">Profile</th>
                  <th className="px-4 py-3 text-left">Branch</th>
                  <th className="px-4 py-3 text-left">Clone</th>
                  <th className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {allRepos.map(repo => (
                  <tr key={repo.id} data-testid="repo-row" className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium text-gray-800 max-w-xs truncate">
                      {repo.url}
                    </td>
                    <td className="px-4 py-3 text-gray-500 text-xs">
                      {(repo as unknown as { profileName: string }).profileName}{' '}
                      <span className="opacity-60">
                        ({(repo as unknown as { profileVersion: string }).profileVersion})
                      </span>
                    </td>
                    <td className="px-4 py-3 text-gray-500">{repo.branch}</td>
                    <td className="px-4 py-3">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                          repo.clone_status === 'cloned'
                            ? 'bg-green-100 text-green-800'
                            : repo.clone_status === 'error'
                            ? 'bg-red-100 text-red-800'
                            : 'bg-yellow-100 text-yellow-800'
                        }`}
                      >
                        {repo.clone_status}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {(repo as unknown as { writable: boolean }).writable ? (
                        <div className="flex items-center justify-end gap-2">
                          <button
                            type="button"
                            onClick={() => handleIndex(repo.id)}
                            disabled={indexingId === repo.id}
                            data-testid={`index-repo-button-${repo.id}`}
                            className="text-xs bg-blue-50 hover:bg-blue-100 disabled:opacity-50 text-blue-700 px-3 py-1.5 rounded-lg transition-colors"
                          >
                            {indexingId === repo.id ? 'Indexing…' : 'Index'}
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(repo.id, repo.url)}
                            disabled={deletingId === repo.id}
                            data-testid={`delete-repo-button-${repo.id}`}
                            className="text-xs bg-red-50 hover:bg-red-100 disabled:opacity-50 text-red-700 px-3 py-1.5 rounded-lg transition-colors"
                          >
                            {deletingId === repo.id ? 'Deleting…' : 'Delete'}
                          </button>
                        </div>
                      ) : (
                        <span
                          data-testid={`repo-readonly-${repo.id}`}
                          className="text-xs text-gray-400 italic"
                          title="Shared repository — managed by an admin"
                        >
                          Read-only
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
