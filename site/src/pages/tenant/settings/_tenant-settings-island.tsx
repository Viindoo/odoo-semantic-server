// SPDX-License-Identifier: AGPL-3.0-or-later
// Tenant Settings React island — per-tenant quota override UI (WI-11)
// Handles editing tenant-scopable settings via /api/tenants/{tid}/settings/{key}.
// Uses the same widget vocabulary as _setting-editor-island.tsx (admin) but with
// the tenant endpoint contract (effective_value / tenant_override / system_default).
import { useState, useCallback } from 'react';
import { submitJson } from '../../../lib/apiClient';
import { flash } from '../../../lib/flash';
import type { TenantSettingDef } from '../../../lib/settings-types';

interface Props {
  tenantId: number;
  initialSettings: TenantSettingDef[];
}

// ─── Duration helpers ─────────────────────────────────────────────────────────

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

// ─── Sub-widgets ──────────────────────────────────────────────────────────────

function NumberWidget({
  value,
  onChange,
  validation,
  isFloat,
}: {
  value: number;
  onChange: (v: number) => void;
  validation: TenantSettingDef['validation'];
  isFloat: boolean;
}) {
  const min = validation?.min;
  const max = validation?.max;
  const [text, setText] = useState(String(value));

  const commit = useCallback(() => {
    const parsed = isFloat ? parseFloat(text) : parseInt(text, 10);
    if (!isNaN(parsed)) {
      const clamped =
        min !== undefined
          ? Math.max(min, max !== undefined ? Math.min(max, parsed) : parsed)
          : parsed;
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
        <span className="text-xs text-gray-400 whitespace-nowrap">
          {min}–{max}
        </span>
      )}
    </div>
  );
}

function DurationWidget({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
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
        onChange={(e) => {
          setText(e.target.value);
          setParseError(false);
        }}
        onBlur={commit}
        className={`w-28 border rounded-lg px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 ${
          parseError
            ? 'border-red-400 focus:ring-red-300'
            : 'border-gray-300 focus:ring-viindoo-primary-deep'
        }`}
      />
      <span className="text-xs text-gray-400">({value}s)</span>
      {parseError && <span className="text-xs text-red-600">Invalid format</span>}
    </div>
  );
}

function BoolWidget({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (v: boolean) => void;
}) {
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

function EnumWidget({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep bg-white"
    >
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function TextWidget({
  value,
  onChange,
  regexHint,
}: {
  value: string;
  onChange: (v: string) => void;
  regexHint?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 border border-gray-300 rounded-lg px-3 py-1.5 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
      />
      {regexHint && (
        <span className="text-xs text-gray-400 font-mono">Pattern: {regexHint}</span>
      )}
    </div>
  );
}

// ─── History drawer ───────────────────────────────────────────────────────────

interface HistoryEntry {
  id: number;
  old_value: { v?: unknown } | null;
  new_value: { v?: unknown } | null;
  changed_by: number | null;
  changed_at: string | null;
  change_reason: string | null;
}

function TenantHistoryDrawer({
  tenantId,
  settingKey,
  onClose,
}: {
  tenantId: number;
  settingKey: string;
  onClose: () => void;
}) {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useState(() => {
    setLoading(true);
    submitJson<HistoryEntry[]>(
      `/api/tenants/${tenantId}/settings/${encodeURIComponent(settingKey)}/history?limit=50`,
      { method: 'GET', stepUp: false },
    ).then((r) => {
      if (r.ok) setEntries(Array.isArray(r.data) ? r.data : []);
      else setError(r.error ?? `HTTP ${r.status}`);
      setLoading(false);
    });
  });

  const fmt = (v: unknown) => {
    if (v === null || v === undefined)
      return <span className="text-gray-400 italic">none</span>;
    const str = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return <span className="font-mono text-xs">{str}</span>;
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end">
      <button
        className="absolute inset-0 bg-black/30"
        onClick={onClose}
        aria-label="Close history"
      />
      <div className="relative z-10 w-full max-w-md h-full bg-white shadow-2xl flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
          <div>
            <h3 className="font-semibold text-gray-900 text-sm">Change History</h3>
            <p className="text-xs text-gray-500 font-mono mt-0.5">{settingKey}</p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none"
          >
            &times;
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading && (
            <p className="text-sm text-gray-400 text-center py-8">Loading history...</p>
          )}
          {error && (
            <p className="text-sm text-red-600 text-center py-8">{error}</p>
          )}
          {!loading && !error && entries.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-8">No history entries.</p>
          )}
          {!loading &&
            entries.map((entry) => {
              const oldVal = entry.old_value?.v ?? entry.old_value;
              const newVal = entry.new_value?.v ?? entry.new_value;
              return (
                <div
                  key={entry.id}
                  className="mb-3 rounded-xl border border-gray-100 bg-gray-50 p-3 text-xs"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-gray-500">
                      {entry.changed_at
                        ? entry.changed_at.slice(0, 19).replace('T', ' ')
                        : 'unknown'}
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
      </div>
    </div>
  );
}

// ─── Main island ──────────────────────────────────────────────────────────────

export default function TenantSettingsIsland({ tenantId, initialSettings }: Props) {
  const [settings, setSettings] = useState<TenantSettingDef[]>(initialSettings);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, unknown>>({});
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [historyKey, setHistoryKey] = useState<string | null>(null);
  const [resettingKey, setResettingKey] = useState<string | null>(null);

  const getDraftValue = (s: TenantSettingDef) => {
    if (editingKey === s.key && s.key in draftValues) return draftValues[s.key];
    return s.effective_value;
  };

  const startEdit = (s: TenantSettingDef) => {
    setEditingKey(s.key);
    // Seed draft from existing tenant override if present, else effective value
    setDraftValues((prev) => ({
      ...prev,
      [s.key]: s.tenant_override !== null ? s.tenant_override : s.effective_value,
    }));
    setReasons((prev) => ({ ...prev, [s.key]: '' }));
    setErrors((prev) => ({ ...prev, [s.key]: '' }));
  };

  const cancelEdit = (key: string) => {
    if (editingKey === key) setEditingKey(null);
  };

  const handleSave = async (s: TenantSettingDef) => {
    const value = draftValues[s.key];
    const reason = (reasons[s.key] ?? '').trim();
    if (!reason || reason.length < 3) {
      setErrors((prev) => ({ ...prev, [s.key]: 'Reason required (min 3 chars).' }));
      return;
    }
    setSaving((prev) => ({ ...prev, [s.key]: true }));
    setErrors((prev) => ({ ...prev, [s.key]: '' }));
    try {
      const r = await submitJson(
        `/api/tenants/${tenantId}/settings/${encodeURIComponent(s.key)}`,
        { method: 'PATCH', body: { value, reason } },
      );
      if (!r.ok) {
        setErrors((prev) => ({ ...prev, [s.key]: r.error! }));
        setSaving((prev) => ({ ...prev, [s.key]: false }));
        return;
      }
      flash(`Setting ${s.key} saved. Effective in ≤60 s.`);
      // Optimistically update local state so the card reflects the new override
      setSettings((prev) =>
        prev.map((item) =>
          item.key === s.key
            ? {
                ...item,
                effective_value: value,
                effective_source: 'tenant_override',
                tenant_override: value,
              }
            : item,
        ),
      );
      setEditingKey(null);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setErrors((prev) => ({ ...prev, [s.key]: msg }));
    } finally {
      setSaving((prev) => ({ ...prev, [s.key]: false }));
    }
  };

  const handleReset = async (s: TenantSettingDef) => {
    if (
      !confirm(
        `Reset "${s.key}" — remove tenant override and fall back to system value (${JSON.stringify(s.system_default)})?`,
      )
    )
      return;
    setResettingKey(s.key);
    try {
      // Content-Type included so Astro's checkOrigin guard (dev/preview/CI
      // proxy) doesn't 403 the reset before it reaches FastAPI. Harmless in
      // prod (nginx bypasses the proxy); the reset POST carries no body.
      const r = await submitJson(
        `/api/tenants/${tenantId}/settings/${encodeURIComponent(s.key)}/reset`,
        { method: 'POST' },
      );
      if (!r.ok) {
        flash(r.error!, { error: true });
      } else {
        flash(`Setting ${s.key} reset to system default.`);
        // Fetch the fresh effective/system values so we reflect any admin changes
        // made to the system default since page-load (C-3 fix).
        // The GET /{key} endpoint returns `system_value` (not `system_default`);
        // we map it to the TenantSettingDef.system_default field.
        // On fetch failure we fall back to the page-load snapshot.
        let freshSystemDefault: unknown = s.system_default;
        let freshEffectiveValue: unknown = s.system_default;
        try {
          const freshR = await submitJson<{ system_value?: unknown; effective_value?: unknown }>(
            `/api/tenants/${tenantId}/settings/${encodeURIComponent(s.key)}`,
            { method: 'GET', stepUp: false },
          );
          if (freshR.ok) {
            // system_value is the live system-level default (no tenant context)
            if ('system_value' in freshR.data) freshSystemDefault = freshR.data.system_value;
            if ('effective_value' in freshR.data) freshEffectiveValue = freshR.data.effective_value;
          }
        } catch { /* fallback to snapshot values already set above */ }
        setSettings((prev) =>
          prev.map((item) =>
            item.key === s.key
              ? {
                  ...item,
                  effective_value: freshEffectiveValue,
                  effective_source: 'system_or_default',
                  tenant_override: null,
                  system_default: freshSystemDefault,
                }
              : item,
          ),
        );
      }
    } catch (e: unknown) {
      flash(String(e), { error: true });
    } finally {
      setResettingKey(null);
    }
  };

  const renderWidget = (
    s: TenantSettingDef,
    value: unknown,
    onChange: (v: unknown) => void,
  ) => {
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
        return <BoolWidget value={Boolean(value)} onChange={onChange} />;
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
            regexHint={s.validation?.regex}
          />
        );
      default:
        return (
          <textarea
            value={
              typeof value === 'object'
                ? JSON.stringify(value, null, 2)
                : String(value ?? '')
            }
            onChange={(e) => {
              try {
                onChange(JSON.parse(e.target.value));
              } catch {
                onChange(e.target.value);
              }
            }}
            rows={3}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
          />
        );
    }
  };

  const hasOverride = (s: TenantSettingDef) => s.effective_source === 'tenant_override';

  return (
    <div>
      {historyKey && (
        <TenantHistoryDrawer
          tenantId={tenantId}
          settingKey={historyKey}
          onClose={() => setHistoryKey(null)}
        />
      )}

      <div className="space-y-3">
        {settings.map((s) => {
          const isEditing = editingKey === s.key;
          const draftVal = getDraftValue(s);
          const overridden = hasOverride(s);

          return (
            <div
              key={s.key}
              className={`bg-white rounded-xl border shadow-sm transition-all ${
                isEditing
                  ? 'border-viindoo-primary ring-1 ring-viindoo-primary/30'
                  : 'border-gray-200'
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
                      {overridden && !isEditing && (
                        <span className="text-xs bg-viindoo-primary/10 text-gray-700 border border-viindoo-primary/20 rounded-full px-2 py-0.5 font-medium">
                          Tenant override
                        </span>
                      )}
                      {!overridden && !isEditing && (
                        <span className="text-xs bg-gray-100 text-gray-500 rounded-full px-2 py-0.5 font-medium">
                          System default
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
                          disabled={!overridden || resettingKey === s.key}
                          className="text-xs px-2 py-1 rounded-lg border border-gray-200 text-gray-500 hover:bg-gray-50 disabled:opacity-40"
                          title="Remove tenant override, revert to system default"
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

                {/* Value display (non-editing) */}
                {!isEditing && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2">
                    <span className="text-xs text-gray-400">Effective:</span>
                    <span className="font-mono text-xs text-gray-700 bg-gray-50 border border-gray-100 px-2 py-0.5 rounded max-w-xs truncate">
                      {JSON.stringify(s.effective_value)}
                    </span>
                    {overridden && (
                      <>
                        <span className="text-xs text-gray-400">System:</span>
                        <span className="font-mono text-xs text-gray-400 max-w-xs truncate">
                          {JSON.stringify(s.system_default)}
                        </span>
                      </>
                    )}
                    {s.updated_at && (
                      <span className="text-xs text-gray-400 ml-auto">
                        last changed {s.updated_at.slice(0, 10)}
                      </span>
                    )}
                  </div>
                )}

                {/* Edit mode */}
                {isEditing && (
                  <div className="mt-3 space-y-3">
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">
                        New tenant value
                        <span className="ml-1 text-gray-400 font-normal">({s.data_type})</span>
                      </label>
                      {renderWidget(s, draftVal, (v) =>
                        setDraftValues((prev) => ({ ...prev, [s.key]: v })),
                      )}
                    </div>
                    <div className="text-xs text-gray-400 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
                      System default: <span className="font-mono">{JSON.stringify(s.system_default)}</span>
                    </div>
                    <div>
                      <label className="block text-xs font-medium text-gray-600 mb-1">
                        Reason <span className="text-red-500">*</span>
                        <span className="ml-1 text-gray-400 font-normal">
                          (required for audit trail)
                        </span>
                      </label>
                      <input
                        type="text"
                        value={reasons[s.key] ?? ''}
                        onChange={(e) =>
                          setReasons((prev) => ({ ...prev, [s.key]: e.target.value }))
                        }
                        placeholder="Why are you overriding this? (min 3 chars)"
                        className="w-full border border-gray-300 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                      />
                    </div>
                    {errors[s.key] && (
                      <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                        {errors[s.key]}
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
                        {saving[s.key] ? 'Saving...' : 'Save Override'}
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
            No tenant-scopable settings available.
          </div>
        )}
      </div>
    </div>
  );
}
