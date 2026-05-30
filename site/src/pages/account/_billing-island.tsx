// SPDX-License-Identifier: AGPL-3.0-or-later
// BillingDashboard React island — subscription state, renewal date, cancel UX.
// IMPORTANT: use className= and htmlFor= (NOT class= or for=) — this is a .tsx React island.
import { useEffect, useState } from 'react';

interface Subscription {
  id: number;
  plan_id: number;
  plan_slug: string | null;
  plan_name: string | null;
  status: string;
  seats: number;
  billing_interval: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_ends_at: string | null;
  cancel_at_period_end: boolean;
  cancelled_at: string | null;
  amount_cents: number | null;
  currency: string | null;
  source: string | null;
}

interface SubscriptionResponse {
  subscriptions: Subscription[];
  manage_url: string;
  error?: string;
}

interface UsageResponse {
  plan: {
    slug: string;
    name: string;
    quota_calls_per_month: number;
    rate_limit_rpm: number;
  } | null;
  current_period: {
    yyyymm: string;
    used: number;
    remaining: number | null;
    percent: number | null;
  } | null;
  error?: string;
}

// ---- helpers ----

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    });
  } catch {
    return iso;
  }
}

function fmtAmount(cents: number | null | undefined, currency: string | null | undefined): string {
  if (cents === null || cents === undefined) return '—';
  const cur = (currency ?? 'USD').toUpperCase();
  // VND is zero-decimal; USD/EUR/GBP are cent-based
  const zeroDecimalCurrencies = new Set(['VND', 'JPY', 'KRW', 'IDR', 'TWD']);
  if (zeroDecimalCurrencies.has(cur)) {
    return `${cents.toLocaleString('en-US')} ${cur}`;
  }
  const amount = cents / 100;
  return `${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur}`;
}

function statusBadge(sub: Subscription): { label: string; className: string } {
  if (sub.cancel_at_period_end) {
    return {
      label: 'Cancelling',
      className: 'bg-yellow-100 text-yellow-800 border border-yellow-200',
    };
  }
  if (sub.status === 'active') {
    return {
      label: 'Active',
      className: 'bg-green-100 text-green-700 border border-green-200',
    };
  }
  if (sub.status === 'trialing') {
    return {
      label: 'Trial',
      className: 'bg-blue-100 text-blue-700 border border-blue-200',
    };
  }
  if (sub.status === 'past_due') {
    return {
      label: 'Past Due',
      className: 'bg-red-100 text-red-700 border border-red-200',
    };
  }
  if (sub.status === 'cancelled' || sub.status === 'canceled') {
    return {
      label: 'Cancelled',
      className: 'bg-gray-100 text-gray-600 border border-gray-200',
    };
  }
  return {
    label: sub.status,
    className: 'bg-gray-100 text-gray-600 border border-gray-200',
  };
}

// ---- main component ----

export default function BillingDashboard() {
  const [subData, setSubData] = useState<SubscriptionResponse | null>(null);
  const [usageData, setUsageData] = useState<UsageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Cancel-dialog state
  const [showCancelDialog, setShowCancelDialog] = useState(false);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelSuccess, setCancelSuccess] = useState(false);

  useEffect(() => {
    const loadAll = async () => {
      try {
        const [subRes, usageRes] = await Promise.allSettled([
          fetch('/api/account/subscription', { credentials: 'include' }),
          fetch('/api/account/usage', { credentials: 'include' }),
        ]);

        if (subRes.status === 'fulfilled') {
          const res = subRes.value;
          if (res.status === 401) {
            window.location.href = '/login?return=/account/billing';
            return;
          }
          if (res.ok) {
            const d = await res.json() as SubscriptionResponse;
            setSubData(d);
          } else {
            setError(`Failed to load subscription data (${res.status}).`);
          }
        } else {
          setError('Could not connect to API server.');
        }

        if (usageRes.status === 'fulfilled' && usageRes.value.ok) {
          const d = await usageRes.value.json() as UsageResponse;
          setUsageData(d);
        }
      } finally {
        setLoading(false);
      }
    };

    loadAll();
    // Refresh every 60 seconds (keep data fresh — E3 pattern)
    const t = setInterval(loadAll, 60000);
    return () => clearInterval(t);
  }, []);

  const handleCancel = async () => {
    setCancelLoading(true);
    setCancelError(null);
    try {
      const res = await fetch('/api/account/subscription/cancel', {
        method: 'POST',
        credentials: 'include',
      });
      const data = await res.json() as {
        status?: string;
        access_until?: string;
        manage_url?: string;
        error?: string;
        detail?: string;
      };

      if (res.ok) {
        setCancelSuccess(true);
        setShowCancelDialog(false);
        // Refresh subscription state to show updated badge
        const refreshRes = await fetch('/api/account/subscription', { credentials: 'include' });
        if (refreshRes.ok) {
          const d = await refreshRes.json() as SubscriptionResponse;
          setSubData(d);
        }
      } else if (res.status === 503 || res.status === 502) {
        // Polar API unavailable — surface manage_url
        const portalUrl = data.manage_url ?? subData?.manage_url ?? 'https://polar.sh/';
        setCancelError(
          `Online cancellation is temporarily unavailable. Please cancel from the ` +
          `<a href="${portalUrl}" target="_blank" rel="noopener" class="underline text-viindoo-primary-text">Polar customer portal</a>.`
        );
      } else if (res.status === 404) {
        setCancelError('No active subscription found to cancel.');
      } else {
        const detail = data.detail || data.error || 'Cancellation failed. Please try again.';
        setCancelError(detail);
      }
    } catch {
      setCancelError('Network error. Please try again or use the Polar portal.');
    } finally {
      setCancelLoading(false);
    }
  };

  // ---- render ----

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-gray-500 text-sm py-8">
        <span className="animate-spin text-viindoo-primary-text">⟳</span>
        Loading billing data…
      </div>
    );
  }

  if (error) {
    return (
      <div
        data-testid="billing-error"
        className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-red-700 text-sm"
      >
        ⚠️ {error}
      </div>
    );
  }

  const subs = subData?.subscriptions ?? [];
  const manageUrl = subData?.manage_url ?? 'https://polar.sh/';
  const activeSub = subs.find(
    (s) => s.status === 'active' || s.status === 'trialing' || s.cancel_at_period_end
  );
  const isFreeTier =
    !activeSub ||
    activeSub.plan_slug === 'free' ||
    activeSub.billing_interval === 'free' ||
    activeSub.billing_interval === null;

  return (
    <div className="space-y-6" data-testid="billing-dashboard">

      {/* Cancel success notice */}
      {cancelSuccess && (
        <div
          data-testid="cancel-success-banner"
          className="bg-yellow-50 border border-yellow-200 rounded-xl px-5 py-4 text-sm text-yellow-800"
          role="status"
        >
          <p className="font-semibold mb-1">Cancellation scheduled</p>
          <p>
            Your subscription has been cancelled. You keep full access until the end of your current billing period.
            No refund is issued for the remaining days.{' '}
            <a
              href={manageUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="underline text-viindoo-primary-text"
            >
              View in Polar portal
            </a>
          </p>
        </div>
      )}

      {/* Subscription card */}
      {activeSub ? (
        <div
          data-testid="subscription-card"
          className="bg-white rounded-xl border border-gray-200 shadow-sm p-6"
        >
          <div className="flex items-start justify-between mb-4 flex-wrap gap-3">
            <div>
              <h2 className="text-xl font-bold text-gray-900">
                {activeSub.plan_name ?? activeSub.plan_slug ?? 'Current Plan'}
              </h2>
              {activeSub.seats > 1 && (
                <p className="text-sm text-gray-500 mt-0.5">{activeSub.seats} seats</p>
              )}
            </div>
            <span
              data-testid="status-badge"
              className={`text-xs font-semibold px-3 py-1 rounded-full ${statusBadge(activeSub).className}`}
            >
              {statusBadge(activeSub).label}
            </span>
          </div>

          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-sm mb-6">
            {activeSub.billing_interval && activeSub.billing_interval !== 'free' && (
              <>
                <div>
                  <dt className="text-gray-400 text-xs uppercase tracking-wide font-medium mb-0.5">
                    Billing cycle
                  </dt>
                  <dd className="text-gray-800 font-medium capitalize">
                    {activeSub.billing_interval}
                  </dd>
                </div>
                {activeSub.amount_cents !== null && (
                  <div>
                    <dt className="text-gray-400 text-xs uppercase tracking-wide font-medium mb-0.5">
                      Amount
                    </dt>
                    <dd className="text-gray-800 font-medium">
                      {fmtAmount(activeSub.amount_cents, activeSub.currency)}
                    </dd>
                  </div>
                )}
              </>
            )}

            {activeSub.trial_ends_at && (
              <div>
                <dt className="text-gray-400 text-xs uppercase tracking-wide font-medium mb-0.5">
                  Trial ends
                </dt>
                <dd className="text-gray-800 font-medium">
                  {fmtDate(activeSub.trial_ends_at)}
                </dd>
              </div>
            )}

            {activeSub.current_period_end && activeSub.billing_interval !== 'free' && (
              <div>
                <dt className="text-gray-400 text-xs uppercase tracking-wide font-medium mb-0.5">
                  {activeSub.cancel_at_period_end ? 'Access until' : 'Renews on'}
                </dt>
                <dd
                  data-testid="renewal-date"
                  className={`font-medium ${
                    activeSub.cancel_at_period_end ? 'text-yellow-700' : 'text-gray-800'
                  }`}
                >
                  {fmtDate(activeSub.current_period_end)}
                </dd>
              </div>
            )}
          </dl>

          {/* Cancel-at-period-end notice */}
          {activeSub.cancel_at_period_end && activeSub.current_period_end && (
            <div
              data-testid="cancellation-notice"
              className="mb-6 bg-yellow-50 border border-yellow-200 rounded-lg px-4 py-3 text-sm text-yellow-800"
            >
              Your subscription is scheduled to cancel on <strong>{fmtDate(activeSub.current_period_end)}</strong>.
              You keep full access until then. No refund for the current period.
            </div>
          )}

          {/* Action buttons */}
          <div className="flex flex-wrap items-center gap-3">
            <a
              href={manageUrl}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="manage-billing-link"
              className="inline-flex items-center gap-2 bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 px-4 py-2 rounded-lg text-sm font-semibold transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
              Manage billing &amp; invoices
            </a>

            {!isFreeTier && !activeSub.cancel_at_period_end && (
              <button
                data-testid="cancel-btn"
                onClick={() => { setShowCancelDialog(true); setCancelError(null); }}
                className="inline-flex items-center gap-2 border border-gray-300 hover:border-red-300 text-gray-600 hover:text-red-600 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
              >
                Cancel subscription
              </button>
            )}
          </div>

          <p className="text-xs text-gray-400 mt-4">
            Invoices and payment history are managed by{' '}
            <a href="https://polar.sh" target="_blank" rel="noopener noreferrer" className="underline">
              Polar
            </a>{' '}
            (your Merchant of Record).
          </p>
        </div>
      ) : (
        /* No active subscription */
        <div
          data-testid="no-subscription-card"
          className="bg-white rounded-xl border border-gray-200 shadow-sm p-8 text-center"
        >
          <p className="text-4xl mb-3">💳</p>
          <p className="text-lg font-semibold text-gray-700">No paid subscription</p>
          <p className="text-sm text-gray-500 mt-2 mb-5">
            You are on the Free plan. Upgrade to unlock higher quota, more repos, and priority support.
          </p>
          <a
            href="/pricing"
            className="inline-block bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 px-5 py-2.5 rounded-lg text-sm font-semibold transition-colors"
          >
            View pricing plans
          </a>
        </div>
      )}

      {/* Quota bar from usage data */}
      {usageData?.current_period && usageData.plan && (
        <div
          data-testid="quota-bar"
          className="bg-white rounded-xl border border-gray-200 shadow-sm p-6"
        >
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-base font-semibold text-gray-800">Current quota usage</h3>
            <a
              href="/account/usage"
              className="text-xs text-viindoo-primary-text hover:text-viindoo-primary-deep transition-colors"
            >
              Full usage details →
            </a>
          </div>

          {usageData.plan.quota_calls_per_month === 0 ? (
            <p className="text-sm text-gray-500">
              Unlimited tier — no monthly cap applied.
            </p>
          ) : (
            <>
              <div className="flex items-end justify-between mb-1.5">
                <span className="text-2xl font-bold text-gray-900">
                  {usageData.current_period.used.toLocaleString()}
                </span>
                <span className="text-sm text-gray-400">
                  of {usageData.plan.quota_calls_per_month.toLocaleString()} calls
                </span>
              </div>
              <div
                className="w-full bg-gray-100 rounded-full h-2 overflow-hidden"
                role="progressbar"
                aria-valuenow={Math.min(100, usageData.current_period.percent ?? 0)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Monthly quota usage"
              >
                <div
                  className={`h-full rounded-full transition-all ${
                    (usageData.current_period.percent ?? 0) >= 100
                      ? 'bg-red-500'
                      : (usageData.current_period.percent ?? 0) >= 80
                      ? 'bg-yellow-400'
                      : 'bg-viindoo-primary'
                  }`}
                  style={{ width: `${Math.min(100, usageData.current_period.percent ?? 0)}%` }}
                />
              </div>
              <p className="text-xs text-gray-400 mt-1">
                {(usageData.current_period.percent ?? 0).toFixed(1)}% used
              </p>
            </>
          )}
        </div>
      )}

      {/* Cancel confirmation dialog (modal-style overlay) */}
      {showCancelDialog && activeSub && (
        <div
          data-testid="cancel-dialog"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="cancel-dialog-title"
        >
          <div className="bg-white rounded-2xl shadow-xl max-w-md w-full p-6">
            <h2 id="cancel-dialog-title" className="text-lg font-bold text-gray-900 mb-3">
              Cancel subscription?
            </h2>
            <p className="text-sm text-gray-600 mb-2">
              Your subscription will remain active until{' '}
              <strong>
                {activeSub.current_period_end
                  ? fmtDate(activeSub.current_period_end)
                  : 'the end of the billing period'}
              </strong>
              . You keep full access until then.
            </p>
            <p className="text-sm text-gray-600 mb-5">
              <strong>No refund</strong> will be issued for the current billing period (partial or remaining days).
              After the period ends, your account automatically reverts to the Free tier.
            </p>

            {cancelError && (
              <div
                data-testid="cancel-dialog-error"
                className="mb-4 bg-red-50 border border-red-200 rounded-lg px-3 py-2.5 text-sm text-red-700"
                dangerouslySetInnerHTML={{ __html: cancelError }}
              />
            )}

            <div className="flex items-center gap-3 justify-end">
              <button
                onClick={() => { setShowCancelDialog(false); setCancelError(null); }}
                disabled={cancelLoading}
                className="px-4 py-2 rounded-lg border border-gray-300 text-sm text-gray-700 font-medium hover:bg-gray-50 transition-colors disabled:opacity-50"
              >
                Keep subscription
              </button>
              <button
                data-testid="confirm-cancel-btn"
                onClick={handleCancel}
                disabled={cancelLoading}
                className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-700 text-white text-sm font-semibold transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {cancelLoading ? 'Cancelling…' : 'Yes, cancel at period end'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
