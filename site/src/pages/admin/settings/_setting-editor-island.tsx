// SPDX-License-Identifier: AGPL-3.0-or-later
// Setting Editor React island — generic widget dispatcher for admin settings (WI-10)
// Handles: NumberSlider, DurationPicker, ToggleSwitch, TextInput, TagListInput
import { useState, useCallback } from 'react';
import { withStepUp } from '../../../lib/mfaStepUp';

type DataType = 'int' | 'float' | 'str' | 'bool' | 'duration_seconds' | 'list_str' | 'struct';

export interface SettingDef {
  key: string;
  value: unknown;
  default_value: unknown;
  data_type: DataType;
  validation: { min?: number; max?: number; enum?: string[]; regex?: string } | null;
  description: string;
  requires_restart: boolean;
  requires_reseed: boolean;
  is_secret: boolean;
  tenant_scopable: boolean;
  effective_source?: string;
  updated_at?: string | null;
  updated_by?: number | null;
  change_reason?: string | null;
}

// ─── Flash helper ────────────────────────────────────────────────────────────

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

// ─── Restart badge ───────────────────────────────────────────────────────────

function RestartBadge({ requiresRestart, requiresReseed }: { requiresRestart: boolean; requiresReseed: boolean }) {
  if (requiresRestart) {
    return (
      <span title="Requires service restart" className="inline-flex items-center gap-1 text-xs font-medium bg-red-100 text-red-700 rounded-full px-2 py-0.5">
        🔴 Restart
      </span>
    );
  }
  if (requiresReseed) {
    return (
      <span title="Requires pattern re-seed (index_profile re-run)" className="inline-flex items-center gap-1 text-xs font-medium bg-yellow-100 text-yellow-700 rounded-full px-2 py-0.5">
        🟡 Worker reload
      </span>
    );
  }
  return (
    <span title="Hot-reload — effective within 60 seconds" className="inline-flex items-center gap-1 text-xs font-medium bg-green-100 text-green-700 rounded-full px-2 py-0.5">
      🟢 Hot-reload
    </span>
  );
}

// ─── Duration helpers ────────────────────────────────────────────────────────

function secondsToHuman(s: number): string {
  if (s <= 0) return '0s';
  if (s % 3600 === 0) return `${s / 3600}h`;
  if (s % 60 === 0) return `${s / 60}m`;
  return `${s}s`;
}

function humanToSeconds(input: string): number | null {
  const trimmed = input.trim().toLowerCase();
  const hMatch = trimmed.match(/^(\d+)h$/);
  if (hMatch) return parseInt(hMatch[1]) * 3600;
  const mMatch = trimmed.match(/^(\d+)m$/);
  if (mMatch) return parseInt(mMatch[1]) * 60;
  const sMatch = trimmed.match(/^(\d+)s?$/);
  if (sMatch) return parseInt(sMatch[1]);
  return null;
}

// ─── Sub-widgets ─────────────────────────────────────────────────────────────

function NumberWidget({
  value,
  onChange,
  validation,
  isFloat,
}: {
  value: number;
  onChange: (v: number) => void;
  validation: SettingDef['validation'];
  isFloat: boolean;
}) {
  const min = validation?.min;
  const max = validation?.max;
  const [text, setText] = useState(String(value));

  const commit = useCallback(() => {
    const parsed = isFloat ? parseFloat(text) : parseInt(text, 10);
    if (!isNaN(parsed)) {
      const clamped = min !== undefined ? Math.max(min, max !== undefined ? Math.min(max, parsed) : parsed) : parsed;
      onChange(clamped);
      setText(String(clamped));
    } else {
      setText(String(value));
    }
  }, [text, isFloat, min, max, onChange, value]);

  return (
    <div className="flex items-center gap-2">
      {min !== undefined && max !== undefined && (
        <input
          type="range"
          min={min}
          max={max}
          step={isFloat ? 0.1 : 1}
          value={value}
          onChange={(e) => {
            const v = isFloat ? parseFloat(e.target.value) : parseInt(e.target.value, 10);
            onChange(v);
            setText(String(v));
          }}
          className="flex-1 h-2 accent-viindoo-primary"
        />
      )}
      <input
        type="number"
        value={text}
        min={min}
        max={max}
        step={isFloat ? 0.1 : 1}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        className="w-24 border border-gray-300 rounded-lg px-2 py-1.5 text-sm text-right text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep font-mono"
      />
      {min !== undefined && max !== undefined && (
        <span className="text-xs text-gray-400 whitespace-nowrap">{min}–{max}</span>
      )}
    </div>
  );
}

function DurationWidget({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const [text, setText] = useState(secondsToHuman(value));
  const [parseError, setParseError] = useState(false);

  const commit = useCallback(() => {
    const parsed = humanToSeconds(text);
    if (parsed !== null && parsed >= 0) {
      setParseError(false);
      onChange(parsed);
      setText(secondsToHuman(parsed));
    } else {
      setParseError(true);
    }
  }, [text, onChange]);

  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        value={text}
        placeholder="e.g. 8h, 30m, 3600"
        onChange={(e) => { setText(e.target.value); setParseError(false); }}
        onBlur={commit}
        className={`w-28 border rounded-lg px-2 py-1.5 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 ${
          parseError ? 'border-red-400 focus:ring-red-300' : 'border-gray-300 focus:ring-viindoo-primary-deep'
        }`}
      />
      <span className="text-xs text-gray-400">({value}s)</span>
      {parseError && <span className="text-xs text-red-600">Invalid format</span>}
    </div>
  );
}

function BoolWidget({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={value}
      onClick={() => onChange(!value)}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep focus:ring-offset-1 ${
        value ? 'bg-viindoo-primary' : 'bg-gray-300'
      }`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
          value ? 'translate-x-6' : 'translate-x-1'
        }`}
      />
    </button>
  );
}

function EnumWidget({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep bg-white"
    >
      {options.map((o) => (
        <option key={o} value={o}>{o}</option>
      ))}
    </select>
  );
}

function TextWidget({
  value,
  onChange,
  isSecret,
  regexHint,
}: {
  value: string;
  onChange: (v: string) => void;
  isSecret: boolean;
  regexHint?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1">
        <input
          type={isSecret && !show ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 border border-gray-300 rounded-lg px-3 py-1.5 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
        />
        {isSecret && (
          <button
            type="button"
            onClick={() => setShow(!show)}
            className="text-xs text-gray-500 hover:text-gray-700 px-2 py-1 border border-gray-200 rounded-lg"
          >
            {show ? 'Hide' : 'Show'}
          </button>
        )}
      </div>
      {regexHint && (
        <span className="text-xs text-gray-400 font-mono">Pattern: {regexHint}</span>
      )}
    </div>
  );
}

function TagListWidget({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [input, setInput] = useState('');

  const add = () => {
    const trimmed = input.trim();
    if (trimmed && !value.includes(trimmed)) {
      onChange([...value, trimmed]);
    }
    setInput('');
  };

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap gap-1 min-h-[2rem] p-1 border border-gray-200 rounded-lg bg-gray-50">
        {value.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 text-xs bg-viindoo-primary/10 text-viindoo-primary-deep border border-viindoo-primary/30 rounded-full px-2 py-0.5 font-mono"
          >
            {tag}
            <button
              type="button"
              onClick={() => onChange(value.filter((t) => t !== tag))}
              className="text-viindoo-primary-deep hover:text-viindoo-secondary leading-none"
            >
              ×
            </button>
          </span>
        ))}
        {value.length === 0 && <span className="text-xs text-gray-400 px-1 py-0.5">No items</span>}
      </div>
      <div className="flex gap-1">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }}
          placeholder="Add item + Enter"
          className="flex-1 border border-gray-300 rounded-lg px-3 py-1 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
        />
        <button
          type="button"
          onClick={add}
          disabled={!input.trim()}
          className="px-3 py-1 text-xs bg-gray-100 hover:bg-gray-200 rounded-lg border border-gray-300 text-gray-700 disabled:opacity-40"
        >
          Add
        </button>
      </div>
    </div>
  );
}

// ─── History Drawer (inline) ─────────────────────────────────────────────────

interface HistoryEntry {
  id: number;
  old_value: { v?: unknown } | null;
  new_value: { v?: unknown } | null;
  changed_by: number | null;
  changed_at: string | null;
  change_reason: string | null;
}

function HistoryDrawer({
  settingKey,
  onClose,
}: {
  settingKey: string;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [undoing, setUndoing] = useState(false);

  useState(() => {
    setLoading(true);
    fetch(`/api/admin/settings/${encodeURIComponent(settingKey)}/history?limit=50`)
      .then((r) => r.json())
      .then((data) => { setEntries(Array.isArray(data) ? data : []); setLoading(false); })
      .catch((e) => { setError(String(e)); setLoading(false); });
  });

  const handleUndo = async () => {
    if (!confirm('Undo last change to this setting? This will revert to the previous value.')) return;
    setUndoing(true);
    try {
      const res = await withStepUp(() => fetch(`/api/admin/settings/${encodeURIComponent(settingKey)}/undo`, {
        method: 'POST',
        credentials: 'include',
      }));
      if (res.ok) {
        flash(`Setting ${settingKey} reverted.`);
        onClose();
        setTimeout(() => location.reload(), 600);
      } else {
        const d = await res.json().catch(() => ({})) as { detail?: string };
        flash(d.detail ?? 'Undo failed.', true);
      }
    } catch (e) {
      flash(String(e), true);
    } finally {
      setUndoing(false);
    }
  };

  const fmt = (v: unknown) => {
    if (v === null || v === undefined) return <span className="text-gray-400 italic">none</span>;
    const str = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return <span className="font-mono text-xs">{str}</span>;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      {/* Backdrop */}
      <button className="absolute inset-0 bg-black/30" onClick={onClose} aria-label="Close history" />
      {/* Drawer panel */}
      <div className="relative z-10 w-full max-w-md h-full bg-white shadow-2xl flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
          <div>
            <h3 className="font-semibold text-gray-900 text-sm">Change History</h3>
            <p className="text-xs text-gray-500 font-mono mt-0.5">{settingKey}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading && <p className="text-sm text-gray-400 text-center py-8">Loading history...</p>}
          {error && <p className="text-sm text-red-600 text-center py-8">{error}</p>}
          {!loading && !error && entries.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-8">No history entries.</p>
          )}
          {!loading && entries.map((entry) => {
            const oldVal = entry.old_value?.v ?? entry.old_value;
            const newVal = entry.new_value?.v ?? entry.new_value;
            return (
              <div key={entry.id} className="mb-3 rounded-xl border border-gray-100 bg-gray-50 p-3 text-xs">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-gray-500">
                    {entry.changed_at ? entry.changed_at.slice(0, 19).replace('T', ' ') : 'unknown'}
                  </span>
                  {entry.changed_by && (
                    <span className="bg-violet-100 text-violet-700 rounded-full px-2 py-0.5 text-xs font-medium">
                      user:{entry.changed_by}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-gray-400">before:</span> {fmt(oldVal)}
                  <span className="text-gray-400">→</span>
                  <span className="text-gray-700 font-semibold">{fmt(newVal)}</span>
                </div>
                {entry.change_reason && (
                  <p className="text-gray-500 italic">{entry.change_reason}</p>
                )}
              </div>
            );
          })}
        </div>

        <div className="px-4 py-3 border-t border-gray-200">
          <button
            onClick={handleUndo}
            disabled={undoing || entries.length === 0}
            className="w-full py-2 rounded-xl text-sm font-medium border border-viindoo-secondary text-viindoo-secondary hover:bg-viindoo-secondary hover:text-white transition-colors disabled:opacity-40"
          >
            {undoing ? 'Reverting...' : 'Undo Last Change'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main SettingEditor component ────────────────────────────────────────────

interface Props {
  settings: SettingDef[];
}

export default function SettingEditorIsland({ settings }: Props) {
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, unknown>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [resettingKey, setResettingKey] = useState<string | null>(null);

  const getDraftValue = (s: SettingDef) => {
    if (editingKey === s.key && s.key in draftValues) return draftValues[s.key];
    return s.value;
  };

  const startEdit = (s: SettingDef) => {
    setEditingKey(s.key);
    setDraftValues((prev) => ({ ...prev, [s.key]: s.value }));
    setReasons((prev) => ({ ...prev, [s.key]: '' }));
    setErrors((prev) => ({ ...prev, [s.key]: '' }));
  };

  const cancelEdit = (key: string) => {
    if (editingKey === key) setEditingKey(null);
  };

  const handleSave = async (s: SettingDef) => {
    const value = draftValues[s.key];
    const reason = (reasons[s.key] ?? '').trim();
    if (!reason || reason.length < 3) {
      setErrors((prev) => ({ ...prev, [s.key]: 'Reason required (min 3 chars).' }));
      return;
    }
    setSaving((prev) => ({ ...prev, [s.key]: true }));
    setErrors((prev) => ({ ...prev, [s.key]: '' }));
    try {
      const res = await withStepUp(() => fetch(`/api/admin/settings/${encodeURIComponent(s.key)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ value, reason }),
      }));
      if (!res.ok) {
        const d = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      flash(`Setting ${s.key} saved. Effective in ≤60 s.`);
      setEditingKey(null);
      setTimeout(() => location.reload(), 600);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setErrors((prev) => ({ ...prev, [s.key]: msg }));
    } finally {
      setSaving((prev) => ({ ...prev, [s.key]: false }));
    }
  };

  const handleReset = async (s: SettingDef) => {
    if (!confirm(`Reset "${s.key}" to default value (${JSON.stringify(s.default_value)})?`)) return;
    setResettingKey(s.key);
    try {
      const res = await withStepUp(() => fetch(`/api/admin/settings/${encodeURIComponent(s.key)}/reset`, {
        method: 'POST',
        credentials: 'include',
      }));
      if (!res.ok) {
        const d = await res.json().catch(() => ({})) as { detail?: string };
        flash(d.detail ?? 'Reset failed.', true);
      } else {
        flash(`Setting ${s.key} reset to default.`);
        setTimeout(() => location.reload(), 600);
      }
    } catch (e: unknown) {
      flash(String(e), true);
    } finally {
      setResettingKey(null);
    }
  };

  const renderWidget = (s: SettingDef, value: unknown, onChange: (v: unknown) => void) => {
    switch (s.data_type) {
      case 'int':
        return (
          <NumberWidget
            value={typeof value === 'number' ? value : Number(value) || 0}
            onChange={onChange}
            validation={s.validation}
            isFloat={false}
          />
        );
      case 'float':
        return (
          <NumberWidget
            value={typeof value === 'number' ? value : Number(value) || 0}
            onChange={onChange}
            validation={s.validation}
            isFloat={true}
          />
        );
      case 'duration_seconds':
        return (
          <DurationWidget
            value={typeof value === 'number' ? value : Number(value) || 0}
            onChange={onChange}
          />
        );
      case 'bool':
        return (
          <BoolWidget
            value={Boolean(value)}
            onChange={onChange}
          />
        );
      case 'str':
        if (s.validation?.enum) {
          return (
            <EnumWidget
              value={String(value ?? '')}
              onChange={onChange}
              options={s.validation.enum}
            />
          );
        }
        return (
          <TextWidget
            value={String(value ?? '')}
            onChange={onChange}
            isSecret={s.is_secret}
            regexHint={s.validation?.regex}
          />
        );
      case 'list_str':
        return (
          <TagListWidget
            value={Array.isArray(value) ? (value as string[]) : []}
            onChange={onChange}
          />
        );
      default:
        return (
          <textarea
            value={typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? '')}
            onChange={(e) => {
              try { onChange(JSON.parse(e.target.value)); } catch { onChange(e.target.value); }
            }}
            rows={3}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
          />
        );
    }
  };

  const isDrift = (s: SettingDef) => {
    const curr = s.value;
    const def = s.default_value;
    return JSON.stringify(curr) !== JSON.stringify(def);
  };

  return (
    <div>
      {historyKey && (
        <HistoryDrawer settingKey={historyKey} onClose={() => setHistoryKey(null)} />
      )}

      <div className="space-y-3">
        {settings.map((s) => {
          const isEditing = editingKey === s.key;
          const draftVal = getDraftValue(s);
          const drift = isDrift(s);

          return (
            <div
              key={s.key}
              className={`bg-white rounded-xl border shadow-sm transition-all ${
                isEditing ? 'border-viindoo-primary ring-1 ring-viindoo-primary/30' : 'border-gray-200'
              }`}
            >
              <div className="px-4 py-3">
                {/* Header row */}
                <div className="flex items-start justify-between gap-3 mb-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <code className="text-xs font-mono font-semibold text-gray-800 bg-gray-100 px-2 py-0.5 rounded">
                        {s.key}
                      </code>
                      <RestartBadge requiresRestart={s.requires_restart} requiresReseed={s.requires_reseed} />
                      {s.tenant_scopable && (
                        <span className="text-xs bg-blue-100 text-blue-700 rounded-full px-2 py-0.5 font-medium">
                          Tenant-scopable
                        </span>
                      )}
                      {drift && !isEditing && (
                        <span className="text-xs bg-amber-100 text-amber-700 rounded-full px-2 py-0.5 font-medium">
                          Custom value
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-gray-500 mt-1">{s.description}</p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {!isEditing && (
                      <>
                        <button
                          onClick={() => setHistoryKey(s.key)}
                          className="text-xs px-2 py-1 rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50"
                        >
                          History
                        </button>
                        <button
                          onClick={() => handleReset(s)}
                          disabled={!drift || resettingKey === s.key}
                          className="text-xs px-2 py-1 rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-40"
                          title="Reset to default"
                        >
                          {resettingKey === s.key ? '...' : 'Reset'}
                        </button>
                        <button
                          onClick={() => startEdit(s)}
                          className="text-xs px-3 py-1 rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
                        >
                          Edit
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {/* Current value display (non-editing) */}
                {!isEditing && (
                  <div className="flex items-center gap-2 mt-2">
                    <span className="text-xs text-gray-400">Current:</span>
                    {s.is_secret ? (
                      <span className="font-mono text-xs text-gray-400">••••••••</span>
                    ) : (
                      <span className="font-mono text-xs text-gray-700 bg-gray-50 border border-gray-100 px-2 py-0.5 rounded max-w-xs truncate">
                        {JSON.stringify(s.value)}
                      </span>
                    )}
                    {drift && (
                      <>
                        <span className="text-xs text-gray-400 mx-1">Default:</span>
                        <span className="font-mono text-xs text-gray-400 max-w-xs truncate">
                          {JSON.stringify(s.default_value)}
                        </span>
                      </>
                    )}
                  </div>
                )}

                {/* Edit mode */}
                {isEditing && (
                  <div className="mt-3 space-y-3">
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">
                        New value
                        <span className="ml-1 text-gray-400 font-normal">({s.data_type})</span>
                      </label>
                      {renderWidget(s, draftVal, (v) =>
                        setDraftValues((prev) => ({ ...prev, [s.key]: v }))
                      )}
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">
                        Reason <span className="text-red-500">*</span>
                        <span className="ml-1 text-gray-400 font-normal">(required for audit trail)</span>
                      </label>
                      <input
                        type="text"
                        value={reasons[s.key] ?? ''}
                        onChange={(e) => setReasons((prev) => ({ ...prev, [s.key]: e.target.value }))}
                        placeholder="Why are you changing this? (min 3 chars)"
                        className="w-full border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                      />
                    </div>
                    {errors[s.key] && (
                      <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                        {errors[s.key]}
                      </p>
                    )}
                    {s.requires_restart && (
                      <p className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                        ⚠️ This setting requires a service restart to take effect.
                      </p>
                    )}
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => cancelEdit(s.key)}
                        className="flex-1 py-1.5 rounded-lg border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => handleSave(s)}
                        disabled={saving[s.key]}
                        className="flex-1 py-1.5 rounded-lg bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50"
                      >
                        {saving[s.key] ? 'Saving...' : 'Save'}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          );
        })}

        {settings.length === 0 && (
          <div className="text-center py-12 text-gray-400 text-sm">
            No settings found in this category.
          </div>
        )}
      </div>
    </div>
  );
}
