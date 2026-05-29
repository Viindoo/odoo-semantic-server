// SPDX-License-Identifier: AGPL-3.0-or-later
// EE Modules Editor React island — admin CRUD for ee_modules guard list (WI-10)
import { useState } from 'react';

interface EEModule {
  id: number;
  name: string;
  since_version: string | null;
  vt_equivalent: string | null;
  description: string | null;
  deprecated: boolean;
  created_at: string | null;
  updated_at: string | null;
}

interface Props {
  initialModules: EEModule[];
  includeDeprecated: boolean;
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

interface AddEditModalProps {
  existing?: EEModule;
  onClose: () => void;
  onSuccess: () => void;
}

function AddEditModal({ existing, onClose, onSuccess }: AddEditModalProps) {
  const isEdit = !!existing;
  const [name, setName] = useState(existing?.name ?? '');
  const [sinceVersion, setSinceVersion] = useState(existing?.since_version ?? '');
  const [vtEquivalent, setVtEquivalent] = useState(existing?.vt_equivalent ?? '');
  const [description, setDescription] = useState(existing?.description ?? '');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const r = reason.trim();
    if (!r || r.length < 3) { setFormError('Reason required (min 3 chars).'); return; }
    setSaving(true);

    const body: Record<string, unknown> = { reason: r };
    if (!isEdit) {
      body.name = name.trim();
      if (sinceVersion.trim()) body.since_version = sinceVersion.trim();
      if (vtEquivalent.trim()) body.vt_equivalent = vtEquivalent.trim();
      if (description.trim()) body.description = description.trim();
    } else {
      if (sinceVersion.trim() !== (existing!.since_version ?? '')) body.since_version = sinceVersion.trim() || null;
      if (vtEquivalent.trim() !== (existing!.vt_equivalent ?? '')) body.vt_equivalent = vtEquivalent.trim() || null;
      if (description.trim() !== (existing!.description ?? '')) body.description = description.trim() || null;
    }

    try {
      const url = isEdit ? `/api/admin/ee-modules/${existing!.id}` : '/api/admin/ee-modules';
      const method = isEdit ? 'PATCH' : 'POST';
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({})) as { detail?: string; error?: string };
      if (res.ok) {
        flash(isEdit ? `Module "${existing!.name}" updated.` : `Module "${name}" added to EE guard list.`);
        onSuccess();
        onClose();
      } else {
        setFormError(String(data.detail ?? data.error ?? `HTTP ${res.status}`));
      }
    } catch (e: unknown) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-bold text-gray-900">
            {isEdit ? `Edit — ${existing!.name}` : 'Add EE Module'}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none p-1">&times;</button>
        </div>

        {formError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
            {formError}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          {!isEdit && (
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Module Name <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                placeholder="e.g. account_accountant"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Since Version</label>
              <input
                type="text"
                value={sinceVersion}
                onChange={(e) => setSinceVersion(e.target.value)}
                placeholder="e.g. 14.0"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">VT Equivalent</label>
              <input
                type="text"
                value={vtEquivalent}
                onChange={(e) => setVtEquivalent(e.target.value)}
                placeholder="e.g. viin_account"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              placeholder="Optional human-readable description"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Reason <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Why is this change being made?"
              required
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
            />
          </div>

          <div className="flex gap-3 pt-1">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50">
              Cancel
            </button>
            <button type="submit" disabled={saving} className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50">
              {saving ? 'Saving...' : (isEdit ? 'Update' : 'Add Module')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function EEModulesEditorIsland({ initialModules, includeDeprecated: initIncludeDeprecated }: Props) {
  const [modules, setModules] = useState<EEModule[]>(initialModules);
  const [includeDeprecated, setIncludeDeprecated] = useState(initIncludeDeprecated);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingModule, setEditingModule] = useState<EEModule | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);

  const reload = async (withDeprecated: boolean) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/admin/ee-modules?include_deprecated=${withDeprecated}`);
      if (res.ok) {
        const data = await res.json() as EEModule[];
        setModules(data);
        setSelectedIds(new Set());
      }
    } catch {
      // silently fail — user sees stale data
    } finally {
      setLoading(false);
    }
  };

  const handleDeprecatedToggle = (checked: boolean) => {
    setIncludeDeprecated(checked);
    reload(checked);
  };

  const handleDelete = async (mod: EEModule) => {
    if (!confirm(`Soft-delete "${mod.name}"? It will be marked deprecated and hidden from the active guard list.`)) return;
    const res = await fetch(`/api/admin/ee-modules/${mod.id}`, {
      method: 'DELETE',
      credentials: 'include',
    });
    if (res.ok) {
      flash(`Module "${mod.name}" soft-deleted.`);
      reload(includeDeprecated);
    } else {
      const d = await res.json().catch(() => ({})) as { detail?: string };
      flash(d.detail ?? 'Delete failed.', true);
    }
  };

  const handleBulkDelete = async () => {
    if (selectedIds.size === 0) return;
    const names = modules.filter((m) => selectedIds.has(m.id)).map((m) => m.name).join(', ');
    if (!confirm(`Soft-delete ${selectedIds.size} module(s): ${names}?`)) return;
    setBulkDeleting(true);
    let failCount = 0;
    for (const id of selectedIds) {
      const res = await fetch(`/api/admin/ee-modules/${id}`, { method: 'DELETE', credentials: 'include' });
      if (!res.ok) failCount++;
    }
    setBulkDeleting(false);
    if (failCount > 0) {
      flash(`${failCount} deletion(s) failed. Others succeeded.`, true);
    } else {
      flash(`${selectedIds.size} module(s) soft-deleted.`);
    }
    setSelectedIds(new Set());
    reload(includeDeprecated);
  };

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const filtered = modules.filter((m) =>
    m.name.toLowerCase().includes(search.toLowerCase()) ||
    (m.description ?? '').toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div>
      {showAddModal && (
        <AddEditModal
          onClose={() => setShowAddModal(false)}
          onSuccess={() => reload(includeDeprecated)}
        />
      )}
      {editingModule && (
        <AddEditModal
          existing={editingModule}
          onClose={() => setEditingModule(null)}
          onSuccess={() => reload(includeDeprecated)}
        />
      )}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search modules..."
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep w-56"
        />
        <label className="flex items-center gap-1.5 text-sm text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={includeDeprecated}
            onChange={(e) => handleDeprecatedToggle(e.target.checked)}
            className="rounded"
          />
          Show deprecated
        </label>
        <div className="flex-1" />
        {selectedIds.size > 0 && (
          <button
            onClick={handleBulkDelete}
            disabled={bulkDeleting}
            className="px-4 py-2 text-sm rounded-lg bg-viindoo-secondary text-white font-medium hover:opacity-90 disabled:opacity-50"
          >
            {bulkDeleting ? 'Deleting...' : `Soft-delete ${selectedIds.size} selected`}
          </button>
        )}
        <button
          onClick={() => setShowAddModal(true)}
          className="px-4 py-2 text-sm rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
        >
          + Add Module
        </button>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-3 py-3 w-8">
                  <input
                    type="checkbox"
                    checked={selectedIds.size === filtered.filter((m) => !m.deprecated).length && filtered.filter((m) => !m.deprecated).length > 0}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedIds(new Set(filtered.filter((m) => !m.deprecated).map((m) => m.id)));
                      } else {
                        setSelectedIds(new Set());
                      }
                    }}
                    className="rounded"
                  />
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Name</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Since</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">VT Equiv.</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Description</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400 text-sm">Loading...</td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400 text-sm">No modules found.</td>
                </tr>
              ) : (
                filtered.map((mod) => (
                  <tr key={mod.id} className={`hover:bg-gray-50 transition-colors ${mod.deprecated ? 'opacity-50' : ''}`}>
                    <td className="px-3 py-3">
                      {!mod.deprecated && (
                        <input
                          type="checkbox"
                          checked={selectedIds.has(mod.id)}
                          onChange={() => toggleSelect(mod.id)}
                          className="rounded"
                        />
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono font-semibold text-gray-700 bg-gray-100 px-2 py-0.5 rounded">
                        {mod.name}
                      </code>
                    </td>
                    <td className="px-4 py-3 text-xs font-mono text-gray-500">{mod.since_version ?? '—'}</td>
                    <td className="px-4 py-3 text-xs font-mono text-gray-500">{mod.vt_equivalent ?? '—'}</td>
                    <td className="px-4 py-3 text-xs text-gray-600 max-w-xs truncate" title={mod.description ?? ''}>
                      {mod.description ?? '—'}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {mod.deprecated ? (
                        <span className="text-xs font-medium text-gray-500 bg-gray-100 rounded-full px-2 py-0.5">Deprecated</span>
                      ) : (
                        <span className="text-xs font-medium text-green-700 bg-green-100 rounded-full px-2 py-0.5">Active</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <div className="flex items-center justify-center gap-1.5">
                        {!mod.deprecated && (
                          <>
                            <button
                              onClick={() => setEditingModule(mod)}
                              className="text-xs px-2 py-1 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50"
                            >
                              Edit
                            </button>
                            <button
                              onClick={() => handleDelete(mod)}
                              className="text-xs px-2 py-1 rounded-lg border border-red-200 text-red-600 hover:bg-red-50"
                            >
                              Delete
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <p className="mt-2 text-xs text-gray-400">
        Soft-delete marks modules as deprecated — they remain in history but are excluded from the active EE guard list.
      </p>
    </div>
  );
}
