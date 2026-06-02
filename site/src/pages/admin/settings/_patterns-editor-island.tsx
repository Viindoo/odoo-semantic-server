// SPDX-License-Identifier: AGPL-3.0-or-later
// Patterns Editor React island — admin pattern catalogue CRUD (WI-10, ADR-0009)
import { useState } from 'react';
import { withStepUp } from '../../../lib/mfaStepUp';

type Language = 'python' | 'xml' | 'js';

interface Pattern {
  pattern_id: string;
  intent_keywords: string[];
  file_ref: string;
  snippet_text: string;
  gotchas: Array<{ title: string; body: string }>;
  odoo_version_min: string;
  odoo_version_max: string | null;
  language: Language;
  core_symbol_names: string[];
  metadata: Record<string, unknown>;
  soft_deleted: boolean;
  created_at: string | null;
  updated_at: string | null;
}

interface Props {
  initialPatterns: Pattern[];
  initialTotal: number;
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

const LANG_COLORS: Record<Language, string> = {
  python: 'bg-blue-100 text-blue-700',
  xml: 'bg-orange-100 text-orange-700',
  js: 'bg-yellow-100 text-yellow-700',
};

// ─── Pattern detail modal ────────────────────────────────────────────────────

interface DetailModalProps {
  pattern: Pattern;
  onClose: () => void;
  onSaved: () => void;
}

function PatternDetailModal({ pattern, onClose, onSaved }: DetailModalProps) {
  const [editing, setEditing] = useState(false);
  const [snippet, setSnippet] = useState(pattern.snippet_text);
  const [fileRef, setFileRef] = useState(pattern.file_ref);
  const [versionMin, setVersionMin] = useState(pattern.odoo_version_min);
  const [versionMax, setVersionMax] = useState(pattern.odoo_version_max ?? '');
  const [language, setLanguage] = useState<Language>(pattern.language);
  const [keywords, setKeywords] = useState(pattern.intent_keywords.join(', '));
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [showFullSnippet, setShowFullSnippet] = useState(false);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const r = reason.trim();
    if (!r || r.length < 3) { setFormError('Reason required (min 3 chars).'); return; }
    setSaving(true);

    const payload: Record<string, unknown> = {
      reason: r,
      snippet_text: snippet,
      file_ref: fileRef,
      odoo_version_min: versionMin,
      odoo_version_max: versionMax.trim() || null,
      language,
      intent_keywords: keywords.split(',').map((k) => k.trim()).filter(Boolean),
    };

    try {
      const res = await withStepUp(() => fetch(`/api/admin/patterns/${encodeURIComponent(pattern.pattern_id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      }));
      const data = await res.json().catch(() => ({})) as { detail?: string; sentinel_sha?: string };
      if (res.ok) {
        flash(`Pattern "${pattern.pattern_id}" updated. Re-embed pending (≤5 min).`);
        onSaved();
        onClose();
      } else {
        setFormError(String(data.detail ?? `HTTP ${res.status}`));
      }
    } catch (e: unknown) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleSoftDelete = async () => {
    if (!confirm(`Soft-delete pattern "${pattern.pattern_id}"? It will be excluded from search results and the active catalogue.`)) return;
    const res = await withStepUp(() => fetch(`/api/admin/patterns/${encodeURIComponent(pattern.pattern_id)}`, {
      method: 'DELETE',
      credentials: 'include',
    }));
    if (res.ok) {
      flash(`Pattern "${pattern.pattern_id}" soft-deleted.`);
      onSaved();
      onClose();
    } else {
      const d = await res.json().catch(() => ({})) as { detail?: string };
      flash(d.detail ?? 'Delete failed.', true);
    }
  };

  const snippetPreview = pattern.snippet_text.slice(0, 200) + (pattern.snippet_text.length > 200 ? '...' : '');

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-end overflow-y-auto">
      <button className="fixed inset-0 bg-black/30" onClick={onClose} aria-label="Close" />
      <div className="relative z-10 w-full max-w-2xl min-h-full bg-white shadow-2xl flex flex-col">
        {/* Header */}
        <div className="flex items-start justify-between px-6 py-4 border-b border-gray-200 sticky top-0 bg-white z-10">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <code className="text-sm font-mono font-bold text-gray-800 bg-gray-100 px-2 py-0.5 rounded">
                {pattern.pattern_id}
              </code>
              <span className={`text-xs font-medium rounded-full px-2 py-0.5 ${LANG_COLORS[pattern.language]}`}>
                {pattern.language}
              </span>
              {pattern.soft_deleted && (
                <span className="text-xs font-medium bg-gray-100 text-gray-500 rounded-full px-2 py-0.5">Deleted</span>
              )}
            </div>
            <p className="text-xs text-gray-500 mt-0.5 font-mono">{pattern.file_ref}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none ml-3 shrink-0">
            &times;
          </button>
        </div>

        <div className="flex-1 p-6 overflow-y-auto">
          {!editing ? (
            /* View mode */
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4 text-xs">
                <div>
                  <span className="font-medium text-gray-600">Version range:</span>
                  <span className="ml-2 font-mono">{pattern.odoo_version_min} – {pattern.odoo_version_max ?? 'latest'}</span>
                </div>
                <div>
                  <span className="font-medium text-gray-600">Updated:</span>
                  <span className="ml-2">{pattern.updated_at?.slice(0, 10) ?? '—'}</span>
                </div>
              </div>

              {pattern.intent_keywords.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-gray-600 mb-1">Keywords</p>
                  <div className="flex flex-wrap gap-1">
                    {pattern.intent_keywords.map((k) => (
                      <span key={k} className="text-xs bg-gray-100 text-gray-700 rounded-full px-2 py-0.5 font-mono">{k}</span>
                    ))}
                  </div>
                </div>
              )}

              <div>
                <div className="flex items-center justify-between mb-1">
                  <p className="text-xs font-medium text-gray-600">Snippet</p>
                  {pattern.snippet_text.length > 200 && (
                    <button
                      onClick={() => setShowFullSnippet(!showFullSnippet)}
                      className="text-xs text-viindoo-primary-text hover:underline"
                    >
                      {showFullSnippet ? 'Collapse' : 'Expand all'}
                    </button>
                  )}
                </div>
                <pre className="bg-gray-900 text-gray-100 rounded-xl p-4 text-xs overflow-x-auto max-h-64 overflow-y-auto font-mono whitespace-pre-wrap">
                  {showFullSnippet ? pattern.snippet_text : snippetPreview}
                </pre>
              </div>

              {pattern.gotchas.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-gray-600 mb-1">Gotchas ({pattern.gotchas.length})</p>
                  <div className="space-y-2">
                    {pattern.gotchas.map((g, i) => (
                      <div key={i} className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs">
                        <p className="font-semibold text-amber-800 mb-0.5">{g.title}</p>
                        <p className="text-amber-700">{g.body}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="flex gap-2 pt-2">
                {!pattern.soft_deleted && (
                  <>
                    <button
                      onClick={() => setEditing(true)}
                      className="px-4 py-2 text-sm rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
                    >
                      Edit
                    </button>
                    <button
                      onClick={handleSoftDelete}
                      className="px-4 py-2 text-sm rounded-lg border border-red-200 text-red-600 hover:bg-red-50 font-medium"
                    >
                      Soft-delete
                    </button>
                  </>
                )}
              </div>
            </div>
          ) : (
            /* Edit mode */
            <form onSubmit={handleSave} className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Language</label>
                  <select
                    value={language}
                    onChange={(e) => setLanguage(e.target.value as Language)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep bg-white"
                  >
                    <option value="python">Python</option>
                    <option value="xml">XML</option>
                    <option value="js">JS</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">File Ref</label>
                  <input
                    type="text"
                    value={fileRef}
                    onChange={(e) => setFileRef(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Version Min</label>
                  <input
                    type="text"
                    value={versionMin}
                    onChange={(e) => setVersionMin(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">Version Max <span className="text-gray-400">(blank=latest)</span></label>
                  <input
                    type="text"
                    value={versionMax}
                    onChange={(e) => setVersionMax(e.target.value)}
                    placeholder="e.g. 17.0"
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Keywords <span className="text-gray-400">(comma-separated)</span></label>
                <input
                  type="text"
                  value={keywords}
                  onChange={(e) => setKeywords(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Snippet</label>
                <textarea
                  value={snippet}
                  onChange={(e) => setSnippet(e.target.value)}
                  rows={12}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
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
                  placeholder="Why is this pattern being updated?"
                  required
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
              </div>

              {formError && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
                  {formError}
                </div>
              )}

              <div className="flex gap-2">
                <button type="button" onClick={() => setEditing(false)} className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50">
                  Cancel
                </button>
                <button type="submit" disabled={saving} className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50">
                  {saving ? 'Saving...' : 'Save Changes'}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Add pattern modal ───────────────────────────────────────────────────────

function AddPatternModal({ onClose, onSuccess }: { onClose: () => void; onSuccess: () => void }) {
  const [patternId, setPatternId] = useState('');
  const [language, setLanguage] = useState<Language>('python');
  const [fileRef, setFileRef] = useState('');
  const [snippet, setSnippet] = useState('');
  const [keywords, setKeywords] = useState('');
  const [versionMin, setVersionMin] = useState('17.0');
  const [versionMax, setVersionMax] = useState('');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const r = reason.trim();
    if (!r || r.length < 3) { setFormError('Reason required (min 3 chars).'); return; }
    setSaving(true);
    try {
      const res = await withStepUp(() => fetch('/api/admin/patterns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          pattern_id: patternId.trim(),
          language,
          file_ref: fileRef.trim(),
          snippet_text: snippet,
          intent_keywords: keywords.split(',').map((k) => k.trim()).filter(Boolean),
          odoo_version_min: versionMin.trim(),
          odoo_version_max: versionMax.trim() || null,
          reason: r,
        }),
      }));
      const data = await res.json().catch(() => ({})) as { detail?: string };
      if (res.ok) {
        flash(`Pattern "${patternId}" created. Re-embed pending.`);
        onSuccess();
        onClose();
      } else {
        setFormError(String(data.detail ?? `HTTP ${res.status}`));
      }
    } catch (e: unknown) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-bold text-gray-900">Add Pattern</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>
        </div>

        {formError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">{formError}</div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Pattern ID <span className="text-red-500">*</span></label>
              <input type="text" value={patternId} onChange={(e) => setPatternId(e.target.value)} required
                placeholder="e.g. sale-order-confirm"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Language <span className="text-red-500">*</span></label>
              <select value={language} onChange={(e) => setLanguage(e.target.value as Language)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep bg-white">
                <option value="python">Python</option>
                <option value="xml">XML</option>
                <option value="js">JS</option>
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">File Ref <span className="text-red-500">*</span></label>
            <input type="text" value={fileRef} onChange={(e) => setFileRef(e.target.value)} required
              placeholder="e.g. addons/sale/models/sale_order.py"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Version Min <span className="text-red-500">*</span></label>
              <input type="text" value={versionMin} onChange={(e) => setVersionMin(e.target.value)} required
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Version Max</label>
              <input type="text" value={versionMax} onChange={(e) => setVersionMax(e.target.value)}
                placeholder="blank = latest"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Keywords <span className="text-gray-400">(comma-separated)</span></label>
            <input type="text" value={keywords} onChange={(e) => setKeywords(e.target.value)}
              placeholder="e.g. sale, confirm, workflow"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Snippet <span className="text-red-500">*</span></label>
            <textarea value={snippet} onChange={(e) => setSnippet(e.target.value)} required rows={8}
              placeholder="Paste the code snippet here..."
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Reason <span className="text-red-500">*</span></label>
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} required
              placeholder="Why is this pattern being added?"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep" />
          </div>

          <div className="flex gap-3 pt-1">
            <button type="button" onClick={onClose} className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50">Cancel</button>
            <button type="submit" disabled={saving} className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50">
              {saving ? 'Creating...' : 'Create Pattern'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Main island ─────────────────────────────────────────────────────────────

const PAGE_SIZE = 50;

export default function PatternsEditorIsland({ initialPatterns, initialTotal }: Props) {
  const [patterns, setPatterns] = useState<Pattern[]>(initialPatterns);
  const [total, setTotal] = useState(initialTotal);
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [languageFilter, setLanguageFilter] = useState<Language | ''>('');
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [selectedPattern, setSelectedPattern] = useState<Pattern | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [pendingReseed, setPendingReseed] = useState(false);

  const load = async (opts: { offset?: number; lang?: Language | ''; deleted?: boolean }) => {
    setLoading(true);
    const lang = opts.lang ?? languageFilter;
    const deleted = opts.deleted ?? includeDeleted;
    const off = opts.offset ?? offset;
    const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(off) });
    if (lang) qs.set('language', lang);
    if (deleted) qs.set('include_deleted', 'true');

    try {
      const res = await fetch(`/api/admin/patterns?${qs.toString()}`);
      if (res.ok) {
        const data = await res.json() as { patterns: Pattern[]; total: number };
        setPatterns(data.patterns);
        setTotal(data.total);
      }
    } catch { /* silent */ }
    finally { setLoading(false); }
  };

  const handleReloadAfterMutation = async () => {
    setPendingReseed(true);
    // Reload at the current offset so pagination context is preserved.
    // Exception: if a delete emptied the current page (patterns came back empty
    // and we're not on page 1), step back one page so the user isn't left
    // staring at a blank table.
    setLoading(true);
    const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (languageFilter) qs.set('language', languageFilter);
    if (includeDeleted) qs.set('include_deleted', 'true');
    let landed = false;
    try {
      const res = await fetch(`/api/admin/patterns?${qs.toString()}`);
      if (res.ok) {
        const data = await res.json() as { patterns: Pattern[]; total: number };
        if (data.patterns.length === 0 && offset > 0) {
          // Current page is now empty (likely a delete) — step back one page.
          const prevOffset = Math.max(0, offset - PAGE_SIZE);
          setOffset(prevOffset);
          landed = true;
          setLoading(false);
          load({ offset: prevOffset });  // fire-and-forget; load() manages its own loading state
        } else {
          setPatterns(data.patterns);
          setTotal(data.total);
        }
      }
    } catch { /* silent — matches existing load() behaviour */ }
    finally {
      if (!landed) setLoading(false);
    }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div>
      {showAdd && (
        <AddPatternModal onClose={() => setShowAdd(false)} onSuccess={handleReloadAfterMutation} />
      )}
      {selectedPattern && (
        <PatternDetailModal
          pattern={selectedPattern}
          onClose={() => setSelectedPattern(null)}
          onSaved={handleReloadAfterMutation}
        />
      )}

      {pendingReseed && (
        <div className="mb-4 bg-amber-50 border border-amber-200 text-amber-800 px-4 py-3 rounded-xl text-sm flex items-center gap-2">
          <span>⏳</span>
          <span>Pending re-embed — changes to patterns trigger pgvector re-seeding. Typically completes in &lt;5 min on the next <code>index_profile()</code> run.</span>
        </div>
      )}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <select
          value={languageFilter}
          onChange={(e) => {
            const val = e.target.value as Language | '';
            setLanguageFilter(val);
            setOffset(0);
            load({ lang: val, offset: 0 });
          }}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep bg-white"
        >
          <option value="">All languages</option>
          <option value="python">Python</option>
          <option value="xml">XML</option>
          <option value="js">JS</option>
        </select>

        <label className="flex items-center gap-1.5 text-sm text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={includeDeleted}
            onChange={(e) => {
              setIncludeDeleted(e.target.checked);
              setOffset(0);
              load({ deleted: e.target.checked, offset: 0 });
            }}
            className="rounded"
          />
          Show deleted
        </label>

        <span className="text-xs bg-violet-100 text-violet-700 px-3 py-1 rounded-full font-medium">
          {total} pattern{total !== 1 ? 's' : ''}
        </span>

        <div className="flex-1" />

        <button
          onClick={() => setShowAdd(true)}
          className="px-4 py-2 text-sm rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
        >
          + Add Pattern
        </button>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Pattern ID</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Lang</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">File Ref</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Snippet Preview</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Versions</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Status</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400 text-sm">Loading...</td>
                </tr>
              ) : patterns.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400 text-sm">No patterns found.</td>
                </tr>
              ) : (
                patterns.map((p) => (
                  <tr
                    key={p.pattern_id}
                    className={`hover:bg-gray-50 transition-colors cursor-pointer ${p.soft_deleted ? 'opacity-50' : ''}`}
                    onClick={() => setSelectedPattern(p)}
                  >
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono font-semibold text-gray-700 bg-gray-100 px-2 py-0.5 rounded">
                        {p.pattern_id}
                      </code>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`text-xs font-medium rounded-full px-2 py-0.5 ${LANG_COLORS[p.language]}`}>
                        {p.language}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs font-mono text-gray-500 max-w-[200px] truncate" title={p.file_ref}>
                      {p.file_ref}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600 max-w-xs">
                      <span className="font-mono">
                        {p.snippet_text.slice(0, 80)}{p.snippet_text.length > 80 ? '...' : ''}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs font-mono text-gray-500 whitespace-nowrap">
                      {p.odoo_version_min}–{p.odoo_version_max ?? 'latest'}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {p.soft_deleted ? (
                        <span className="text-xs font-medium bg-gray-100 text-gray-500 rounded-full px-2 py-0.5">Deleted</span>
                      ) : (
                        <span className="text-xs font-medium bg-green-100 text-green-700 rounded-full px-2 py-0.5">Active</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center" onClick={(e) => e.stopPropagation()}>
                      <button
                        onClick={() => setSelectedPattern(p)}
                        className="text-xs px-3 py-1 rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
                      >
                        View
                      </button>
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
                onClick={() => { const o = Math.max(0, offset - PAGE_SIZE); setOffset(o); load({ offset: o }); }}
                disabled={offset === 0 || loading}
                className="px-3 py-1 text-xs rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
              >
                Prev
              </button>
              <button
                onClick={() => { const o = offset + PAGE_SIZE; setOffset(o); load({ offset: o }); }}
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
