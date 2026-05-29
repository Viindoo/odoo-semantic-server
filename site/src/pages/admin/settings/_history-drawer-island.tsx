// SPDX-License-Identifier: AGPL-3.0-or-later
// History Drawer React island — standalone slide-out history panel (WI-10)
// Used by [category].astro as a per-key history trigger.
import { useState, useEffect } from 'react';
import { withStepUp } from '../../../lib/mfaStepUp';

interface HistoryEntry {
  id: number;
  old_value: { v?: unknown } | unknown;
  new_value: { v?: unknown } | unknown;
  changed_by: number | null;
  changed_at: string | null;
  change_reason: string | null;
}

interface Props {
  settingKey: string;
  triggerSelector?: string; // CSS selector of external button that opens this drawer
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

function extractValue(v: unknown): unknown {
  if (v !== null && typeof v === 'object' && 'v' in (v as Record<string, unknown>)) {
    return (v as { v: unknown }).v;
  }
  return v;
}

function fmt(v: unknown) {
  const extracted = extractValue(v);
  if (extracted === null || extracted === undefined) {
    return <span className="text-gray-400 italic">none</span>;
  }
  const str = typeof extracted === 'object' ? JSON.stringify(extracted) : String(extracted);
  return <span className="font-mono text-xs break-all">{str}</span>;
}

export default function HistoryDrawerIsland({ settingKey, triggerSelector }: Props) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);

  // Listen for external trigger button if provided
  useEffect(() => {
    if (!triggerSelector) return;
    const btn = document.querySelector(triggerSelector);
    if (!btn) return;
    const handler = () => setOpen(true);
    btn.addEventListener('click', handler);
    return () => btn.removeEventListener('click', handler);
  }, [triggerSelector]);

  // Load history when opened
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    fetch(`/api/admin/settings/${encodeURIComponent(settingKey)}/history?limit=50`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: unknown) => {
        setEntries(Array.isArray(data) ? data as HistoryEntry[] : []);
        setLoading(false);
      })
      .catch((e: unknown) => {
        setError(String(e));
        setLoading(false);
      });
  }, [open, settingKey]);

  const handleUndo = async () => {
    if (!confirm(`Undo last change to "${settingKey}"? This reverts to the previous value.`)) return;
    setUndoing(true);
    try {
      const res = await withStepUp(() => fetch(`/api/admin/settings/${encodeURIComponent(settingKey)}/undo`, {
        method: 'POST',
        credentials: 'include',
      }));
      if (res.ok) {
        flash(`Setting ${settingKey} reverted to previous value.`);
        setOpen(false);
        setTimeout(() => location.reload(), 600);
      } else {
        const d = await res.json().catch(() => ({})) as { detail?: string };
        flash(d.detail ?? 'Undo failed.', true);
      }
    } catch (e: unknown) {
      flash(String(e), true);
    } finally {
      setUndoing(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      {/* Backdrop */}
      <button
        className="absolute inset-0 bg-black/30 cursor-default"
        onClick={() => setOpen(false)}
        aria-label="Close history drawer"
      />

      {/* Drawer panel */}
      <div className="relative z-10 w-full max-w-md h-full bg-white shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
          <div>
            <h3 className="font-semibold text-gray-900 text-sm">Change History</h3>
            <code className="text-xs text-gray-500 font-mono mt-0.5 block">{settingKey}</code>
          </div>
          <button
            onClick={() => setOpen(false)}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none p-1"
          >
            &times;
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4">
          {loading && (
            <p className="text-sm text-gray-400 text-center py-8">Loading history...</p>
          )}
          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
              {error}
            </p>
          )}
          {!loading && !error && entries.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-8">No history entries yet.</p>
          )}
          {!loading && !error && entries.map((entry) => (
            <div
              key={entry.id}
              className="mb-3 rounded-xl border border-gray-100 bg-gray-50 p-3 text-xs"
            >
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-gray-500">
                  {entry.changed_at
                    ? new Date(entry.changed_at).toLocaleString('en-GB', {
                        dateStyle: 'short',
                        timeStyle: 'medium',
                      })
                    : 'unknown time'}
                </span>
                {entry.changed_by !== null && (
                  <span className="bg-violet-100 text-violet-700 rounded-full px-2 py-0.5 text-xs font-medium">
                    user:{entry.changed_by}
                  </span>
                )}
              </div>
              <div className="flex items-start gap-1.5 flex-wrap mb-1">
                <span className="text-gray-400 shrink-0">before:</span>
                <span className="text-gray-500">{fmt(entry.old_value)}</span>
                <span className="text-gray-400 shrink-0">→</span>
                <span className="font-semibold text-gray-800">{fmt(entry.new_value)}</span>
              </div>
              {entry.change_reason && (
                <p className="text-gray-500 italic mt-0.5 line-clamp-2">
                  Reason: {entry.change_reason}
                </p>
              )}
            </div>
          ))}
        </div>

        {/* Footer action */}
        <div className="px-4 py-3 border-t border-gray-200 space-y-2">
          <button
            onClick={handleUndo}
            disabled={undoing || loading || entries.length === 0}
            className="w-full py-2 rounded-xl text-sm font-medium border border-viindoo-secondary text-viindoo-secondary hover:bg-viindoo-secondary hover:text-white transition-colors disabled:opacity-40"
          >
            {undoing ? 'Reverting...' : 'Undo Last Change'}
          </button>
          <button
            onClick={() => setOpen(false)}
            className="w-full py-2 rounded-xl text-sm font-medium border border-gray-200 text-gray-600 hover:bg-gray-50 transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
