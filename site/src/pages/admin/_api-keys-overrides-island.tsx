// SPDX-License-Identifier: AGPL-3.0-or-later
// ApiKeysOverridesIsland — singleton React island for setting per-key overrides.
//
// Singleton-modal pattern: one island instance is mounted at the bottom of the
// api-keys.astro page. Each row in the Active Keys table has a button with
// `data-overrides-trigger` and data attributes carrying the key context. The
// island listens for clicks on those buttons, reads the attributes, populates
// state, and shows the modal.
//
// Props (passed from Astro): none required at mount time — all data flows in
// via DOM events (data-overrides-trigger delegation).
//
// API: PATCH /api/admin/api-keys/{key_id}/plan
//   body: { plan_id, rate_limit_override: number | null, quota_override: number | null }
//   200 → window.location.reload()
//   4xx/5xx → inline error display

import { useState, useEffect } from 'react';

function flash(msg: string, isError = false) {
  const el = document.querySelector('[data-testid="flash-banner"]') as HTMLElement | null;
  if (!el) return;
  el.textContent = msg;
  el.className = `fixed top-4 right-4 z-50 px-5 py-3 rounded-xl shadow-lg text-sm font-medium border ${
    isError ? 'bg-red-50 border-red-300 text-red-800' : 'bg-green-50 border-green-300 text-green-800'
  }`;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 5000);
}

type OverridesState = {
  keyId: number;
  keyName: string;
  planId: number;
  rateOverride: string;   // empty string = null/unset
  quotaOverride: string;  // empty string = null/unset
};

const EMPTY: OverridesState = {
  keyId: 0,
  keyName: '',
  planId: 0,
  rateOverride: '',
  quotaOverride: '',
};

export default function ApiKeysOverridesIsland() {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<OverridesState>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Listen for clicks on any [data-overrides-trigger] button in the document.
  // Data attributes expected:
  //   data-key-id         (number as string)
  //   data-key-name       (string)
  //   data-current-plan-id (number as string)
  //   data-rate           (number as string or empty)
  //   data-quota          (number as string or empty)
  useEffect(() => {
    function handleTrigger(e: MouseEvent) {
      const btn = (e.target as Element).closest('[data-overrides-trigger]') as HTMLElement | null;
      if (!btn) return;
      const keyId = parseInt(btn.dataset.keyId ?? '0', 10);
      const keyName = btn.dataset.keyName ?? `Key #${keyId}`;
      const planId = parseInt(btn.dataset.currentPlanId || '0', 10);
      const rateRaw = btn.dataset.rate ?? '';
      const quotaRaw = btn.dataset.quota ?? '';
      setState({
        keyId,
        keyName,
        planId,
        rateOverride: rateRaw === 'null' || rateRaw === '' ? '' : rateRaw,
        quotaOverride: quotaRaw === 'null' || quotaRaw === '' ? '' : quotaRaw,
      });
      setFormError(null);
      setOpen(true);
    }
    document.addEventListener('click', handleTrigger);
    return () => document.removeEventListener('click', handleTrigger);
  }, []);

  function handleClose() {
    setOpen(false);
    setState(EMPTY);
    setFormError(null);
  }

  function parseOverride(val: string): number | null {
    if (val.trim() === '') return null;
    const n = parseInt(val, 10);
    return isNaN(n) ? null : n;
  }

  function validateOverrides(): string | null {
    if (state.rateOverride.trim() !== '') {
      const n = parseInt(state.rateOverride, 10);
      if (isNaN(n) || n < 0) return 'Rate limit override must be a non-negative integer.';
    }
    if (state.quotaOverride.trim() !== '') {
      const n = parseInt(state.quotaOverride, 10);
      if (isNaN(n) || n < 0) return 'Quota override must be a non-negative integer.';
    }
    if (state.planId === 0) return 'Cannot save: plan id unknown. Reload the page and try again.';
    return null;
  }

  async function handleSave() {
    const validationError = validateOverrides();
    if (validationError) {
      setFormError(validationError);
      return;
    }
    setLoading(true);
    setFormError(null);
    try {
      const body: Record<string, unknown> = {
        plan_id: state.planId,
        rate_limit_override: parseOverride(state.rateOverride),
        quota_override: parseOverride(state.quotaOverride),
      };
      const res = await fetch(`/api/admin/api-keys/${state.keyId}/plan`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        flash('Overrides saved.');
        handleClose();
        setTimeout(() => window.location.reload(), 800);
      } else {
        const data = await res.json().catch(() => ({})) as Record<string, unknown>;
        setFormError(String((data as { detail?: string; error?: string }).detail ?? (data as { detail?: string; error?: string }).error ?? `Error ${res.status}`));
      }
    } catch (err) {
      setFormError(String(err));
    } finally {
      setLoading(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label={`Overrides for key ${state.keyName}`}
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-lg font-bold text-gray-900">Per-Key Overrides</h2>
            <p className="text-xs text-gray-500 mt-0.5 truncate max-w-[200px]">{state.keyName}</p>
          </div>
          <button
            type="button"
            onClick={handleClose}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        <p className="text-xs text-gray-500 mb-4">
          Leave blank to use plan default. Set 0 = hard block (zero calls allowed). For unlimited access, assign the &lsquo;Unlimited&rsquo; plan instead. Both fields are optional.
        </p>

        {formError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
            {formError}
          </div>
        )}

        <div className="space-y-4">
          {/* Rate limit override */}
          <div>
            <label htmlFor="ov-rate" className="block text-xs font-medium text-gray-700 mb-1">
              Rate Limit Override <span className="text-gray-400 font-normal">(req/min)</span>
            </label>
            <div className="flex gap-2">
              <input
                id="ov-rate"
                type="number"
                min="0"
                step="1"
                placeholder="blank = plan default"
                value={state.rateOverride}
                onChange={(e) => setState((s) => ({ ...s, rateOverride: e.target.value }))}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
              />
              <button
                type="button"
                onClick={() => setState((s) => ({ ...s, rateOverride: '' }))}
                className="text-xs px-2 py-1 rounded-lg border border-gray-300 text-gray-600 hover:bg-gray-100"
                title="Clear (use plan default)"
              >
                Clear
              </button>
            </div>
          </div>

          {/* Quota override */}
          <div>
            <label htmlFor="ov-quota" className="block text-xs font-medium text-gray-700 mb-1">
              Monthly Quota Override <span className="text-gray-400 font-normal">(calls/month)</span>
            </label>
            <div className="flex gap-2">
              <input
                id="ov-quota"
                type="number"
                min="0"
                step="1"
                placeholder="blank = plan default"
                value={state.quotaOverride}
                onChange={(e) => setState((s) => ({ ...s, quotaOverride: e.target.value }))}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
              />
              <button
                type="button"
                onClick={() => setState((s) => ({ ...s, quotaOverride: '' }))}
                className="text-xs px-2 py-1 rounded-lg border border-gray-300 text-gray-600 hover:bg-gray-100"
                title="Clear (use plan default)"
              >
                Clear
              </button>
            </div>
          </div>
        </div>

        <div className="flex gap-3 mt-6">
          <button
            type="button"
            onClick={handleClose}
            className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={loading}
            className="flex-1 py-2 rounded-xl bg-viindoo-primary text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {loading ? 'Saving...' : 'Save Overrides'}
          </button>
        </div>
      </div>
    </div>
  );
}
