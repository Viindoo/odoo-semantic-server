// SPDX-License-Identifier: AGPL-3.0-or-later
// Plan Tier Editor React island — admin plans CRUD (WI-10, ADR-0039)
import { useState } from 'react';

interface Plan {
  id: number;
  slug: string;
  display_name: string;
  quota_calls_per_month: number;
  rate_limit_rpm: number;
  seat_limit: number | null;
  is_public: boolean;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
}

interface Props {
  initialPlans: Plan[];
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

function fmtQuota(n: number): string {
  if (n === 0) return 'Unlimited';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

interface EditState {
  display_name: string;
  quota_calls_per_month: string;
  rate_limit_rpm: string;
  seat_limit: string;
  is_public: boolean;
  reason: string;
}

function EditModal({
  plan,
  onClose,
  onSaved,
}: {
  plan: Plan;
  onClose: () => void;
  onSaved: (updated: Plan) => void;
}) {
  const [form, setForm] = useState<EditState>({
    display_name: plan.display_name,
    quota_calls_per_month: String(plan.quota_calls_per_month),
    rate_limit_rpm: String(plan.rate_limit_rpm),
    seat_limit: plan.seat_limit !== null ? String(plan.seat_limit) : '',
    is_public: plan.is_public,
    reason: '',
  });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const field = <K extends keyof EditState>(key: K, val: EditState[K]) =>
    setForm((prev) => ({ ...prev, [key]: val }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);

    const reason = form.reason.trim();
    if (!reason || reason.length < 3) {
      setFormError('Reason is required (min 3 chars).');
      return;
    }

    const newQuota = parseInt(form.quota_calls_per_month, 10);
    const newRpm = parseInt(form.rate_limit_rpm, 10);
    if (isNaN(newQuota) || newQuota < 0) {
      setFormError('Quota must be a non-negative integer.');
      return;
    }
    if (isNaN(newRpm) || newRpm < 1) {
      setFormError('RPM must be at least 1.');
      return;
    }

    // Warn on >50% drop
    const quotaDrop = plan.quota_calls_per_month > 0 && newQuota < plan.quota_calls_per_month * 0.5;
    const rpmDrop = plan.rate_limit_rpm > 0 && newRpm < plan.rate_limit_rpm * 0.5;
    if (quotaDrop || rpmDrop) {
      const fields = [quotaDrop && 'quota', rpmDrop && 'RPM'].filter(Boolean).join(' and ');
      if (!confirm(`Warning: you are reducing ${fields} by more than 50% for plan "${plan.display_name}". Confirm?`)) {
        return;
      }
    }

    const payload: Record<string, unknown> = {
      display_name: form.display_name,
      quota_calls_per_month: newQuota,
      rate_limit_rpm: newRpm,
      is_public: form.is_public,
      reason,
    };
    const seatRaw = form.seat_limit.trim();
    if (seatRaw !== '') {
      const seatN = parseInt(seatRaw, 10);
      if (isNaN(seatN) || seatN < 1) {
        setFormError('Seat limit must be a positive integer or blank.');
        return;
      }
      payload.seat_limit = seatN;
    }

    setSaving(true);
    try {
      const res = await fetch(`/api/admin/plans/${encodeURIComponent(plan.slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({})) as Record<string, unknown>;
      if (res.ok) {
        flash(`Plan "${plan.display_name}" updated. Changes propagate in ≤60 s.`);
        onSaved({ ...plan, ...payload, quota_calls_per_month: newQuota, rate_limit_rpm: newRpm });
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
          <div>
            <h2 className="text-lg font-bold text-gray-900">Edit Plan</h2>
            <code className="text-xs text-gray-500 font-mono">{plan.slug}</code>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none p-1">
            &times;
          </button>
        </div>

        {formError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
            {formError}
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Display Name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              value={form.display_name}
              onChange={(e) => field('display_name', e.target.value)}
              required
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Monthly Quota <span className="text-xs text-gray-400">(0=unlimited)</span>
              </label>
              <input
                type="number"
                min={0}
                value={form.quota_calls_per_month}
                onChange={(e) => field('quota_calls_per_month', e.target.value)}
                required
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Rate Limit (RPM) <span className="text-red-500">*</span>
              </label>
              <input
                type="number"
                min={1}
                value={form.rate_limit_rpm}
                onChange={(e) => field('rate_limit_rpm', e.target.value)}
                required
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Seat Limit <span className="text-xs text-gray-400">(blank = no limit)</span>
            </label>
            <input
              type="number"
              min={1}
              value={form.seat_limit}
              onChange={(e) => field('seat_limit', e.target.value)}
              placeholder="No limit"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
            />
          </div>

          <div className="flex items-center gap-2">
            <input
              id={`plan-public-${plan.slug}`}
              type="checkbox"
              checked={form.is_public}
              onChange={(e) => field('is_public', e.target.checked)}
              className="rounded"
            />
            <label htmlFor={`plan-public-${plan.slug}`} className="text-sm text-gray-700">
              Public (visible in pricing page)
            </label>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Reason <span className="text-red-500">*</span>
              <span className="ml-1 text-gray-400 font-normal">(audit trail)</span>
            </label>
            <input
              type="text"
              value={form.reason}
              onChange={(e) => field('reason', e.target.value)}
              placeholder="Why are you updating this plan?"
              required
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary"
            />
          </div>

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 py-2 rounded-xl bg-viindoo-primary text-white text-sm font-medium hover:opacity-90 disabled:opacity-50"
            >
              {saving ? 'Saving...' : 'Save Plan'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function PlanTierEditorIsland({ initialPlans }: Props) {
  const [plans, setPlans] = useState<Plan[]>(initialPlans);
  const [editingPlan, setEditingPlan] = useState<Plan | null>(null);

  const handleSaved = (updated: Plan) => {
    setPlans((prev) => prev.map((p) => (p.slug === updated.slug ? { ...p, ...updated } : p)));
  };

  return (
    <div>
      {editingPlan && (
        <EditModal
          plan={editingPlan}
          onClose={() => setEditingPlan(null)}
          onSaved={handleSaved}
        />
      )}

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Slug</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Display Name</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Monthly Quota</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">RPM</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Seats</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Public</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {plans.length === 0 ? (
                <tr>
                  <td colSpan={7} className="px-4 py-8 text-center text-gray-400 text-sm">
                    No plans found.
                  </td>
                </tr>
              ) : (
                plans.map((plan) => (
                  <tr key={plan.slug} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono font-semibold text-gray-700 bg-gray-100 px-2 py-0.5 rounded">
                        {plan.slug}
                      </code>
                    </td>
                    <td className="px-4 py-3 font-medium text-gray-800">{plan.display_name}</td>
                    <td className="px-4 py-3 text-right font-mono text-sm text-gray-700">
                      {fmtQuota(plan.quota_calls_per_month)}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-sm text-gray-700">
                      {plan.rate_limit_rpm}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-sm text-gray-600">
                      {plan.seat_limit ?? '—'}
                    </td>
                    <td className="px-4 py-3 text-center">
                      {plan.is_public ? (
                        <span className="text-xs font-medium text-green-700 bg-green-100 rounded-full px-2 py-0.5">Yes</span>
                      ) : (
                        <span className="text-xs font-medium text-gray-500 bg-gray-100 rounded-full px-2 py-0.5">No</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-center">
                      <button
                        onClick={() => setEditingPlan(plan)}
                        className="text-xs px-3 py-1 rounded-lg bg-viindoo-primary text-white font-medium hover:opacity-90"
                      >
                        Edit
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <p className="mt-3 text-xs text-gray-400">
        Plan tier creation (POST /api/admin/plans) is deferred to Phase 2. Contact engineering to add new tiers.
      </p>
    </div>
  );
}
