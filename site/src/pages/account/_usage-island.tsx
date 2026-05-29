// SPDX-License-Identifier: AGPL-3.0-or-later
// UsageDashboard React island — plan + quota usage + 6-month history.
// IMPORTANT: use className= and htmlFor= (NOT class= or for=) — this is a .tsx React island.
import { useEffect, useState } from 'react';

interface Plan {
  slug: string;
  name: string;
  quota_calls_per_month: number;
  rate_limit_rpm: number;
}

interface CurrentPeriod {
  yyyymm: string;
  used: number;
  remaining: number | null;  // null = unlimited (quota == 0)
  percent: number | null;
}

interface HistoryEntry {
  period: string;
  used: number;
}

interface UsageResponse {
  plan: Plan | null;
  current_period: CurrentPeriod | null;
  history: HistoryEntry[];
  error?: string;
}

function formatPeriod(yyyymm: string): string {
  if (yyyymm.length !== 6) return yyyymm;
  const year = yyyymm.slice(0, 4);
  const month = parseInt(yyyymm.slice(4, 6), 10);
  const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${names[month - 1] ?? yyyymm.slice(4, 6)} ${year}`;
}

export default function UsageDashboard() {
  const [data, setData] = useState<UsageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/account/usage', { credentials: 'include' })
      .then(async (res) => {
        if (res.status === 401) {
          window.location.href = '/admin/login?return=/account/usage';
          return null;
        }
        if (!res.ok) {
          throw new Error(`Request failed (${res.status})`);
        }
        return res.json() as Promise<UsageResponse>;
      })
      .then((d) => {
        if (d) {
          if (d.error) setError(d.error);
          else setData(d);
        }
      })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-gray-500 text-sm py-8">
        <span className="animate-spin text-viindoo-primary-text">⟳</span>
        Loading usage data…
      </div>
    );
  }

  if (error) {
    return (
      <div
        data-testid="usage-error"
        className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-red-700 text-sm"
      >
        ⚠️ {error}
      </div>
    );
  }

  if (!data || !data.plan) {
    return (
      <div
        data-testid="usage-no-key"
        className="bg-white rounded-xl border border-gray-200 shadow-sm py-16 text-center"
      >
        <p className="text-4xl mb-3">🔑</p>
        <p className="text-lg font-semibold text-gray-700">No API key associated with your account yet.</p>
        <p className="text-sm text-gray-500 mt-2 mb-4">
          Generate an API key to start tracking usage.
        </p>
        <a
          href="/account/api-keys"
          className="inline-block bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 px-4 py-2 rounded-lg text-sm font-semibold transition-colors"
        >
          Create your first API key
        </a>
      </div>
    );
  }

  const { plan, current_period, history } = data;
  const isUnlimited = plan.quota_calls_per_month === 0;
  const percent = current_period?.percent ?? 0;
  const isWarning = !isUnlimited && percent >= 80;
  const isOver = !isUnlimited && percent >= 100;

  // Bar chart: scale each bar relative to the max used across history
  const maxUsed = Math.max(...(history ?? []).map((h) => h.used), 1);

  return (
    <div className="space-y-6" data-testid="usage-dashboard">
      {/* Plan card */}
      <div
        data-testid="plan-card"
        className="bg-white rounded-xl border border-gray-200 shadow-sm p-6"
      >
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-xl font-bold text-gray-900">{plan.name}</h2>
          <span className="bg-viindoo-secondary/10 text-viindoo-secondary text-xs font-semibold px-2.5 py-1 rounded-full uppercase tracking-wide">
            {plan.slug}
          </span>
        </div>
        <p className="text-gray-500 text-sm">
          {isUnlimited
            ? 'Unlimited MCP tool calls per month'
            : `${plan.quota_calls_per_month.toLocaleString()} calls / month`}
          {' · '}
          {plan.rate_limit_rpm} req/min
        </p>
        {/*
          M10B P0 multi-key disclosure (Wave 2 integration review ISSUE-3):
          the /api/account/usage endpoint resolves a single primary key
          (oldest by id) per user. Surfacing the limitation in the UI
          prevents silent under-reporting for users who hold more than
          one key. Per-key breakdown is deferred to M10B P1.
        */}
        <p
          data-testid="primary-key-hint"
          className="text-gray-400 text-xs mt-1.5"
        >
          Showing usage for your primary API key.
        </p>
      </div>

      {/* Current period progress */}
      {current_period && (
        <div
          data-testid="current-period-card"
          className="bg-white rounded-xl border border-gray-200 shadow-sm p-6"
        >
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-gray-800">
              Current period
            </h3>
            <span className="text-xs text-gray-400 font-mono">
              {formatPeriod(current_period.yyyymm)}
            </span>
          </div>

          <div className="flex items-end justify-between mb-2">
            <span className="text-3xl font-bold text-gray-900">
              {current_period.used.toLocaleString()}
            </span>
            <span className="text-sm text-gray-400">
              {isUnlimited ? 'calls (no cap)' : `of ${plan.quota_calls_per_month.toLocaleString()}`}
            </span>
          </div>

          {!isUnlimited && (
            <>
              <div
                className="w-full bg-gray-100 rounded-full h-2.5 overflow-hidden mb-2"
                role="progressbar"
                aria-valuenow={Math.min(100, percent)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Quota usage"
              >
                <div
                  className={`h-full rounded-full transition-all ${
                    isOver
                      ? 'bg-red-500'
                      : isWarning
                      ? 'bg-yellow-400'
                      : 'bg-viindoo-primary'
                  }`}
                  style={{ width: `${Math.min(100, percent)}%` }}
                />
              </div>
              <p className="text-xs text-gray-400">
                {current_period.remaining !== null
                  ? `${current_period.remaining.toLocaleString()} calls remaining`
                  : '0 calls remaining'}
                {' · '}
                {percent.toFixed(1)}% used
              </p>

              {isWarning && (
                <div
                  data-testid="quota-warning-cta"
                  className={`mt-4 rounded-lg p-3 border ${
                    isOver
                      ? 'bg-red-50 border-red-200'
                      : 'bg-yellow-50 border-yellow-200'
                  }`}
                >
                  <p className={`text-sm font-semibold mb-1 ${isOver ? 'text-red-700' : 'text-yellow-800'}`}>
                    {isOver ? 'Monthly quota exhausted' : 'Approaching monthly quota'}
                  </p>
                  <p className="text-xs text-gray-500">
                    <a
                      href="/pricing"
                      className="text-viindoo-primary-text hover:text-viindoo-primary-deep underline"
                    >
                      Upgrade your plan
                    </a>{' '}
                    to increase the monthly call limit.
                  </p>
                </div>
              )}
            </>
          )}

          {isUnlimited && (
            <p className="text-xs text-gray-400 mt-1">Unlimited tier — no monthly cap applied.</p>
          )}
        </div>
      )}

      {/* 6-month history bar chart */}
      {history && history.length > 0 && (
        <div
          data-testid="history-card"
          className="bg-white rounded-xl border border-gray-200 shadow-sm p-6"
        >
          <h3 className="text-base font-semibold text-gray-800 mb-4">Usage history</h3>
          <div className="space-y-3">
            {history.map((h) => {
              const barWidth = maxUsed > 0 ? (h.used / maxUsed) * 100 : 0;
              const isCurrentPeriod = h.period === current_period?.yyyymm;
              return (
                <div key={h.period} className="flex items-center gap-3">
                  <span className="text-xs text-gray-400 w-16 shrink-0">
                    {formatPeriod(h.period)}
                  </span>
                  <div className="flex-1 bg-gray-100 rounded-full h-2 overflow-hidden">
                    <div
                      className={`h-full rounded-full ${
                        isCurrentPeriod ? 'bg-viindoo-primary' : 'bg-viindoo-primary/40'
                      }`}
                      style={{ width: `${barWidth}%` }}
                    />
                  </div>
                  <span className="text-xs text-gray-500 w-20 text-right shrink-0">
                    {h.used.toLocaleString()}
                    {isCurrentPeriod && (
                      <span className="ml-1 text-viindoo-primary-text font-semibold">·</span>
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Upgrade CTA for non-unlimited plans */}
      {!isUnlimited && (
        <div
          data-testid="upgrade-cta"
          className="bg-viindoo-primary/5 border border-viindoo-primary/20 rounded-xl p-5 flex items-center justify-between gap-4"
        >
          <div>
            <p className="text-sm font-semibold text-gray-800">Need more capacity?</p>
            <p className="text-xs text-gray-500 mt-0.5">
              Upgrade to a higher plan for more calls per month and a faster rate limit.
            </p>
          </div>
          <a
            href="/pricing"
            className="shrink-0 inline-block bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 px-4 py-2 rounded-lg text-sm font-semibold transition-colors"
          >
            View plans
          </a>
        </div>
      )}
    </div>
  );
}
