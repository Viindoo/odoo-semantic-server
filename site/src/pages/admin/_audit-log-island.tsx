// SPDX-License-Identifier: AGPL-3.0-or-later
// Audit Log React island — admin-only viewer (W3 C)
// Handles: filter by action/actor, pagination.
import { useState } from 'react';
import { submitJson } from '../../lib/apiClient';

type AuditEntry = {
  id: number;
  ts: string | null;
  actor: string;
  action: string;
  target: string | null;
  success: boolean | null;
  detail: Record<string, unknown> | null;
};

interface Props {
  initialEntries: AuditEntry[];
  initialTotal: number;
}

const PAGE_SIZE = 50;

async function fetchAuditLog(
  params: { action?: string; actor?: string; limit: number; offset: number }
): Promise<{ entries: AuditEntry[]; total: number } | { error: string }> {
  const qs = new URLSearchParams();
  if (params.action) qs.set('action', params.action);
  if (params.actor) qs.set('actor', params.actor);
  qs.set('limit', String(params.limit));
  qs.set('offset', String(params.offset));
  try {
    const r = await submitJson<{ entries: AuditEntry[]; total: number }>(
      `/api/admin/audit-log?${qs.toString()}`,
      { method: 'GET', stepUp: false },
    );
    if (!r.ok) return { error: r.error! };
    return r.data;
  } catch (e) {
    return { error: String(e) };
  }
}

function StatusBadge({ success }: { success: boolean | null }) {
  if (success === null) return null;
  return (
    <span
      className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
        success ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
      }`}
    >
      {success ? 'ok' : 'fail'}
    </span>
  );
}

export default function AuditLogIsland({ initialEntries, initialTotal }: Props) {
  const [entries, setEntries] = useState<AuditEntry[]>(initialEntries);
  const [total, setTotal] = useState(initialTotal);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [actionFilter, setActionFilter] = useState('');
  const [actorFilter, setActorFilter] = useState('');
  const [offset, setOffset] = useState(0);

  async function load(opts: { action?: string; actor?: string; offset?: number }) {
    setLoading(true);
    setError(null);
    const result = await fetchAuditLog({
      action: opts.action ?? actionFilter,
      actor: opts.actor ?? actorFilter,
      limit: PAGE_SIZE,
      offset: opts.offset ?? offset,
    });
    setLoading(false);
    if ('error' in result) {
      setError(result.error);
    } else {
      setEntries(result.entries);
      setTotal(result.total);
    }
  }

  function handleFilterSubmit(e: React.FormEvent) {
    e.preventDefault();
    setOffset(0);
    load({ action: actionFilter, actor: actorFilter, offset: 0 });
  }

  function handlePrev() {
    const newOffset = Math.max(0, offset - PAGE_SIZE);
    setOffset(newOffset);
    load({ offset: newOffset });
  }

  function handleNext() {
    const newOffset = offset + PAGE_SIZE;
    setOffset(newOffset);
    load({ offset: newOffset });
  }

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div>
      {/* Filter bar */}
      <form onSubmit={handleFilterSubmit} className="flex flex-wrap gap-3 mb-4 items-end">
        <div>
          <label htmlFor="audit-action-filter" className="block text-xs font-medium text-gray-600 mb-1">
            Action
          </label>
          <input
            id="audit-action-filter"
            type="text"
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
            placeholder="e.g. user.login"
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep w-44"
          />
        </div>
        <div>
          <label htmlFor="audit-actor-filter" className="block text-xs font-medium text-gray-600 mb-1">
            Actor
          </label>
          <input
            id="audit-actor-filter"
            type="text"
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
            placeholder="e.g. user:1"
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep w-44"
          />
        </div>
        <button
          type="submit"
          disabled={loading}
          className="px-4 py-2 text-sm font-medium rounded-lg bg-viindoo-primary text-viindoo-bg-0 hover:opacity-90 disabled:opacity-50"
        >
          {loading ? 'Loading...' : 'Filter'}
        </button>
        <button
          type="button"
          onClick={() => {
            setActionFilter('');
            setActorFilter('');
            setOffset(0);
            load({ action: '', actor: '', offset: 0 });
          }}
          className="px-4 py-2 text-sm font-medium rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50"
        >
          Clear
        </button>
      </form>

      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-xl text-sm">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">ID</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Timestamp</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Actor</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Action</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Target</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {entries.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">
                    {loading ? 'Loading...' : 'No audit entries found.'}
                  </td>
                </tr>
              ) : (
                entries.map((entry) => (
                  <tr key={entry.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 text-gray-500 font-mono text-xs">{entry.id}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs whitespace-nowrap">
                      {entry.ts ? entry.ts.slice(0, 19).replace('T', ' ') : '-'}
                    </td>
                    <td className="px-4 py-3 text-gray-700 font-mono text-xs">{entry.actor}</td>
                    <td className="px-4 py-3 text-gray-800 font-semibold text-xs">{entry.action}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{entry.target ?? '-'}</td>
                    <td className="px-4 py-3">
                      <StatusBadge success={entry.success} />
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div className="px-4 py-3 border-t border-gray-200 flex items-center justify-between">
            <span className="text-xs text-gray-500">
              Page {currentPage} of {totalPages} ({total} total)
            </span>
            <div className="flex gap-2">
              <button
                onClick={handlePrev}
                disabled={offset === 0 || loading}
                className="px-3 py-1 text-xs rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Prev
              </button>
              <button
                onClick={handleNext}
                disabled={offset + PAGE_SIZE >= total || loading}
                className="px-3 py-1 text-xs rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
