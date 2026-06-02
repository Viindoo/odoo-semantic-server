// SPDX-License-Identifier: AGPL-3.0-or-later
// Plan Tier Editor React island — admin plans CRUD (WI-10, ADR-0039)
import { useState } from 'react';
import { withStepUp } from '../../../lib/mfaStepUp';

interface Plan {
  id: number;
  slug: string;
  display_name: string;
  quota_calls_per_month: number;
  rate_limit_rpm: number;
  seat_limit: number | null;
  is_public: boolean;
  is_archived: boolean;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  // Pricing fields (C1 — ADR-0039)
  price_cents: number | null;
  currency: string | null;
  billing_interval: 'free' | 'monthly' | 'annual' | 'one_time' | null;
  trial_days: number | null;
  prices: Record<string, number> | null;  // per-currency map e.g. {"USD": 1900} — USD only for now (multi-currency deferred)
  pricing_model: 'flat' | 'per_seat' | null;  // m13_015 — "flat" or "per_seat"
  min_seats: number | null;  // m13_016 — per-plan display SSOT; null = no minimum
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
  is_archived: boolean;
  reason: string;
  // Pricing fields
  price_cents: string;
  currency: string;
  billing_interval: string;
  trial_days: string;
  prices_json: string;  // JSON textarea; parsed before submit
  pricing_model: string;  // "flat" or "per_seat"
  min_seats: string;      // integer or blank (blank = null, no minimum)
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
    is_archived: plan.is_archived,
    reason: '',
    price_cents: plan.price_cents !== null ? String(plan.price_cents) : '',
    currency: plan.currency ?? 'USD',
    billing_interval: plan.billing_interval ?? 'free',
    trial_days: plan.trial_days !== null ? String(plan.trial_days) : '',
    prices_json: plan.prices ? JSON.stringify(plan.prices, null, 2) : '{}',
    pricing_model: plan.pricing_model ?? 'flat',
    min_seats: plan.min_seats !== null && plan.min_seats !== undefined ? String(plan.min_seats) : '',
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

    // Validate + parse prices JSON textarea
    let parsedPrices: Record<string, number> | undefined;
    const pricesRaw = form.prices_json.trim();
    if (pricesRaw && pricesRaw !== '{}') {
      try {
        parsedPrices = JSON.parse(pricesRaw) as Record<string, number>;
        if (typeof parsedPrices !== 'object' || Array.isArray(parsedPrices)) {
          setFormError('Prices must be a JSON object e.g. {"USD": 1900}');
          return;
        }
      } catch {
        setFormError('Prices JSON is invalid. Example: {"USD": 1900}');
        return;
      }
    }

    // NOT NULL columns: clearing a field cannot mean "send null" — the DB would
    // reject it (or, worse, the old payload silently kept the prior value by
    // omitting the field). A blank optional field maps to its explicit DB default
    // so the cleared intent actually persists; required fields block submit with a
    // clear error. Only min_seats is nullable (blank -> explicit null). See WI-1.

    // currency — REQUIRED, NOT NULL. Must be a 3-letter ISO code.
    const currencyRaw = form.currency.trim().toUpperCase();
    if (currencyRaw.length !== 3) {
      setFormError('Currency is required (3-letter ISO code).');
      return;
    }

    // isPaid drives the "paid plans require a price" + "per-seat needs seat_limit" rules.
    const isPaid =
      form.billing_interval === 'monthly' ||
      form.billing_interval === 'annual' ||
      form.billing_interval === 'one_time';

    // price_cents — NOT NULL (default 0). blank -> 0; non-empty validate >= 0.
    let priceCents = 0;
    const priceCentsRaw = form.price_cents.trim();
    if (priceCentsRaw !== '') {
      const pc = parseInt(priceCentsRaw, 10);
      if (isNaN(pc) || pc < 0) {
        setFormError('Price (cents) must be a non-negative integer.');
        return;
      }
      priceCents = pc;
    }

    // prices — NOT NULL (default {}). blank/{} -> {}; else the validated parsedPrices.
    const pricesValue: Record<string, number> = parsedPrices ?? {};

    // A paid plan must carry a price somewhere (price_cents OR prices map).
    if (isPaid && priceCents === 0 && Object.keys(pricesValue).length === 0) {
      setFormError('Paid plans require a price.');
      return;
    }

    // trial_days — NOT NULL (default 0). blank -> 0; non-empty validate 0..365.
    let trialDays = 0;
    const trialRaw = form.trial_days.trim();
    if (trialRaw !== '') {
      const td = parseInt(trialRaw, 10);
      if (isNaN(td) || td < 0 || td > 365) {
        setFormError('Trial days must be between 0 and 365.');
        return;
      }
      trialDays = td;
    }

    // seat_limit — NOT NULL (default 1) and NOT nullable, so there is no
    // "clear to no-limit" state. Per-seat plans REQUIRE a value; for any other
    // plan a blank field means "leave the current value unchanged" — we omit it
    // from the payload (exclude_unset on the backend preserves the DB value)
    // instead of silently overwriting a non-1 limit (e.g. team=20) with 1.
    let seatLimitToSend: number | undefined;
    const seatRaw = form.seat_limit.trim();
    if (form.pricing_model === 'per_seat' && seatRaw === '') {
      setFormError('Seat limit is required for per-seat plans.');
      return;
    }
    if (seatRaw !== '') {
      const seatN = parseInt(seatRaw, 10);
      if (isNaN(seatN) || seatN < 1) {
        setFormError('Seat limit must be a positive integer or blank.');
        return;
      }
      seatLimitToSend = seatN;
    }

    const payload: Record<string, unknown> = {
      display_name: form.display_name,
      quota_calls_per_month: newQuota,
      rate_limit_rpm: newRpm,
      is_public: form.is_public,
      is_archived: form.is_archived,
      billing_interval: form.billing_interval || undefined,
      currency: currencyRaw,
      price_cents: priceCents,
      prices: pricesValue,
      trial_days: trialDays,
      reason,
    };
    // Blank seat_limit preserves the current DB value (NOT NULL, no cleared state).
    if (seatLimitToSend !== undefined) payload.seat_limit = seatLimitToSend;

    // Pricing model (flat | per_seat) — always included when present
    if (form.pricing_model === 'flat' || form.pricing_model === 'per_seat') {
      payload.pricing_model = form.pricing_model;
    }

    // Min seats — per-plan display SSOT (m13_016); blank = null (no minimum).
    // This is the ONLY nullable column: clearing it sends explicit null.
    const minSeatsRaw = form.min_seats.trim();
    if (minSeatsRaw !== '') {
      const ms = parseInt(minSeatsRaw, 10);
      if (isNaN(ms) || ms < 1) {
        setFormError('Min seats must be a positive integer or blank (no minimum).');
        return;
      }
      payload.min_seats = ms;
    } else {
      payload.min_seats = null;
    }

    setSaving(true);
    try {
      const res = await withStepUp(() => fetch(`/api/admin/plans/${encodeURIComponent(plan.slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(payload),
      }));
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
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
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
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
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
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Seat Limit <span className="text-xs text-gray-400">(blank = keep current)</span>
            </label>
            <input
              type="number"
              min={1}
              value={form.seat_limit}
              onChange={(e) => field('seat_limit', e.target.value)}
              placeholder="No limit"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
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

          <div className="flex items-center gap-2">
            <input
              id={`plan-archived-${plan.slug}`}
              type="checkbox"
              checked={form.is_archived}
              onChange={(e) => field('is_archived', e.target.checked)}
              className="rounded"
            />
            <label htmlFor={`plan-archived-${plan.slug}`} className="text-sm text-gray-700">
              Archived (hidden from all selection)
            </label>
          </div>

          {/* Pricing section */}
          <div className="border-t border-gray-100 pt-3">
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Pricing</p>

            <div className="mb-3">
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Pricing Model
                <span className="ml-1 text-gray-400 font-normal">(flat = fixed price; per_seat = price × seats)</span>
              </label>
              <select
                value={form.pricing_model}
                onChange={(e) => field('pricing_model', e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              >
                <option value="flat">flat — fixed price</option>
                <option value="per_seat">per_seat — price × seats</option>
              </select>
            </div>

            <div className="mb-3">
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Min Seats (display)
                <span className="ml-1 text-gray-400 font-normal">
                  (blank = no minimum; synced with billing.team_min_seats enforcement setting)
                </span>
              </label>
              <input
                type="number"
                min={1}
                value={form.min_seats}
                onChange={(e) => field('min_seats', e.target.value)}
                placeholder="e.g. 3 (team) or blank"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Price (cents / display currency)
                  <span className="ml-1 text-gray-400 font-normal">(blank = no change)</span>
                </label>
                <input
                  type="number"
                  min={0}
                  value={form.price_cents}
                  onChange={(e) => field('price_cents', e.target.value)}
                  placeholder="e.g. 1900"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Currency <span className="text-xs text-gray-400">(ISO-4217)</span>
                </label>
                <input
                  type="text"
                  maxLength={3}
                  value={form.currency}
                  onChange={(e) => field('currency', e.target.value.toUpperCase())}
                  placeholder="USD"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 mt-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Billing Interval
                </label>
                <select
                  value={form.billing_interval}
                  onChange={(e) => field('billing_interval', e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                >
                  <option value="free">free</option>
                  <option value="monthly">monthly</option>
                  <option value="annual">annual</option>
                  <option value="one_time">one_time</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Trial Days <span className="text-xs text-gray-400">(0-365, blank=none)</span>
                </label>
                <input
                  type="number"
                  min={0}
                  max={365}
                  value={form.trial_days}
                  onChange={(e) => field('trial_days', e.target.value)}
                  placeholder="0"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                />
              </div>
            </div>
            <div className="mt-3">
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Per-currency prices (JSON)
                <span className="ml-1 text-gray-400 font-normal">
                  e.g. {`{"USD": 1900}`} — multi-currency deferred, USD only for now
                </span>
              </label>
              <textarea
                rows={3}
                value={form.prices_json}
                onChange={(e) => field('prices_json', e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep resize-none"
              />
            </div>
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
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
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
              className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50"
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
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Min Seats</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Price</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Model</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Public</th>
                <th className="px-4 py-3 text-center text-xs font-semibold text-gray-500 uppercase tracking-wide">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {plans.length === 0 ? (
                <tr>
                  <td colSpan={10} className="px-4 py-8 text-center text-gray-400 text-sm">
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
                    <td className="px-4 py-3 text-right font-mono text-sm text-gray-600">
                      {plan.min_seats ?? '—'}
                    </td>
                    <td className="px-4 py-3 text-right font-mono text-sm text-gray-600">
                      {plan.price_cents !== null
                        ? `${plan.price_cents} ${plan.currency ?? ''}`
                        : '—'}
                    </td>
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono text-gray-600">
                        {plan.pricing_model ?? 'flat'}
                      </code>
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
                        className="text-xs px-3 py-1 rounded-lg bg-viindoo-primary text-viindoo-bg-0 font-medium hover:opacity-90"
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
