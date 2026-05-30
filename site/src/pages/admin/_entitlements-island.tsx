// SPDX-License-Identifier: AGPL-3.0-or-later
// EntitlementsIsland — singleton React island for admin entitlement CRUD (M10B P1, ADR-0039)
//
// Singleton-modal pattern: one island instance is mounted at the bottom of the
// entitlements.astro page. Buttons in SubscriptionsTable carry data-ent-*-trigger
// attributes; this island listens for clicks, reads those attrs, and shows the
// correct modal (Grant, Update, Revoke confirm).
//
// ALL mutations go through `withStepUp()` — the backend's grant/update/revoke
// routes require fresh MFA (require_admin_with_fresh_mfa).
//
// APIs:
//   POST   /api/admin/entitlements                       body: GrantBody
//   PATCH  /api/admin/entitlements/{external_ref}        body: UpdateBody
//   POST   /api/admin/entitlements/{external_ref}/revoke (no body)

import { useState, useEffect } from 'react';
import { withStepUp } from '../../lib/mfaStepUp';

// ---------------------------------------------------------------------------
// Flash helper (shared doc-level flash banner rendered by the Astro page)
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// Supported plan slugs (static list for the dropdown — admin context only)
// The canonical source is the plans table; this list covers the common slugs.
// Integrator: extend if custom plans are added.
// ---------------------------------------------------------------------------
const KNOWN_STATUSES = [
  'pending', 'active', 'past_due', 'cancelled', 'expired', 'trialing', 'refunded',
] as const;

// ---------------------------------------------------------------------------
// Modal types
// ---------------------------------------------------------------------------
type ModalMode = 'closed' | 'grant' | 'update';

type GrantState = {
  email: string;
  plan_slug: string;
  seats: string;
  source: 'admin' | 'promo';
};

type UpdateState = {
  external_ref: string;
  plan_slug: string;
  status: string;
  seats: string;
};

const EMPTY_GRANT: GrantState = { email: '', plan_slug: '', seats: '1', source: 'admin' };
const EMPTY_UPDATE: UpdateState = { external_ref: '', plan_slug: '', status: '', seats: '' };

// ---------------------------------------------------------------------------
// Island component
// ---------------------------------------------------------------------------
export default function EntitlementsIsland() {
  const [mode, setMode] = useState<ModalMode>('closed');
  const [grant, setGrant] = useState<GrantState>(EMPTY_GRANT);
  const [update, setUpdate] = useState<UpdateState>(EMPTY_UPDATE);
  const [loading, setLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // -------------------------------------------------------------------------
  // Event delegation — listen for trigger buttons from SubscriptionsTable
  // -------------------------------------------------------------------------
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      const target = e.target as Element;

      // Grant button: data-ent-grant-trigger (set by the page header button)
      const grantBtn = target.closest('[data-ent-grant-trigger]') as HTMLElement | null;
      if (grantBtn) {
        setGrant(EMPTY_GRANT);
        setFormError(null);
        setMode('grant');
        return;
      }

      // Update button: data-ent-update-trigger (set by SubscriptionsTable row)
      const updateBtn = target.closest('[data-ent-update-trigger]') as HTMLElement | null;
      if (updateBtn) {
        setUpdate({
          external_ref: updateBtn.dataset.externalRef ?? '',
          plan_slug: updateBtn.dataset.planSlug ?? '',
          status: updateBtn.dataset.status ?? '',
          seats: updateBtn.dataset.seats ?? '1',
        });
        setFormError(null);
        setMode('update');
        return;
      }

      // Revoke button: data-ent-revoke-trigger (set by SubscriptionsTable row)
      const revokeBtn = target.closest('[data-ent-revoke-trigger]') as HTMLElement | null;
      if (revokeBtn) {
        const ref = revokeBtn.dataset.externalRef ?? '';
        const email = revokeBtn.dataset.buyerEmail ?? ref;
        if (!confirm(`Revoke subscription for "${email}"? This will cancel the subscription and downgrade the linked API key to the free plan.`)) return;
        void handleRevoke(ref);
      }
    }

    document.addEventListener('click', handleClick);
    return () => document.removeEventListener('click', handleClick);
  }, []);

  // -------------------------------------------------------------------------
  // Close
  // -------------------------------------------------------------------------
  function handleClose() {
    setMode('closed');
    setGrant(EMPTY_GRANT);
    setUpdate(EMPTY_UPDATE);
    setFormError(null);
  }

  // -------------------------------------------------------------------------
  // Grant
  // -------------------------------------------------------------------------
  async function handleGrant() {
    if (!grant.email.trim()) { setFormError('Email is required.'); return; }
    if (!grant.plan_slug.trim()) { setFormError('Plan slug is required.'); return; }
    const seats = parseInt(grant.seats, 10);
    if (isNaN(seats) || seats < 1) { setFormError('Seats must be a positive integer.'); return; }

    setLoading(true);
    setFormError(null);
    try {
      const res = await withStepUp(() =>
        fetch('/api/admin/entitlements', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            email: grant.email.trim(),
            plan_slug: grant.plan_slug.trim(),
            seats,
            source: grant.source,
          }),
        })
      );
      if (res.ok) {
        flash('Entitlement granted.');
        handleClose();
        setTimeout(() => window.location.reload(), 800);
      } else {
        const data = await res.json().catch(() => ({})) as { detail?: string; error?: string };
        setFormError(data.detail ?? data.error ?? `Error ${res.status}`);
      }
    } catch (err) {
      setFormError(String(err));
    } finally {
      setLoading(false);
    }
  }

  // -------------------------------------------------------------------------
  // Update
  // -------------------------------------------------------------------------
  async function handleUpdate() {
    if (!update.external_ref) { setFormError('No subscription selected.'); return; }
    // At least one field must be set
    const hasChange = update.plan_slug.trim() || update.status.trim() || update.seats.trim();
    if (!hasChange) { setFormError('Provide at least one field to update.'); return; }

    const body: Record<string, unknown> = {};
    if (update.plan_slug.trim()) body.plan_slug = update.plan_slug.trim();
    if (update.status.trim()) body.status = update.status.trim();
    if (update.seats.trim()) {
      const seats = parseInt(update.seats, 10);
      if (isNaN(seats) || seats < 1) { setFormError('Seats must be a positive integer.'); return; }
      body.seats = seats;
    }

    setLoading(true);
    setFormError(null);
    try {
      const ref = encodeURIComponent(update.external_ref);
      const res = await withStepUp(() =>
        fetch(`/api/admin/entitlements/${ref}`, {
          method: 'PATCH',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
      );
      if (res.ok) {
        flash('Subscription updated.');
        handleClose();
        setTimeout(() => window.location.reload(), 800);
      } else {
        const data = await res.json().catch(() => ({})) as { detail?: string; error?: string };
        setFormError(data.detail ?? data.error ?? `Error ${res.status}`);
      }
    } catch (err) {
      setFormError(String(err));
    } finally {
      setLoading(false);
    }
  }

  // -------------------------------------------------------------------------
  // Revoke (no modal — confirm() inline, called from event handler)
  // -------------------------------------------------------------------------
  async function handleRevoke(externalRef: string) {
    try {
      const ref = encodeURIComponent(externalRef);
      const res = await withStepUp(() =>
        fetch(`/api/admin/entitlements/${ref}/revoke`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
        })
      );
      if (res.ok) {
        flash('Subscription revoked.');
        setTimeout(() => window.location.reload(), 800);
      } else {
        const data = await res.json().catch(() => ({})) as { detail?: string; error?: string };
        flash(data.detail ?? data.error ?? `Revoke failed (${res.status})`, true);
      }
    } catch (err) {
      flash(String(err), true);
    }
  }

  // -------------------------------------------------------------------------
  // Render — nothing visible when closed
  // -------------------------------------------------------------------------
  if (mode === 'closed') return null;

  // Shared modal shell
  const isGrant = mode === 'grant';
  const title = isGrant ? 'Grant Entitlement' : 'Update Subscription';
  const submitLabel = isGrant ? 'Grant' : 'Save Changes';
  const onSubmit = isGrant ? handleGrant : handleUpdate;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm mx-4 p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-bold text-gray-900">{title}</h2>
          <button
            type="button"
            onClick={handleClose}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {/* Inline error */}
        {formError && (
          <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
            {formError}
          </div>
        )}

        {/* --- Grant form --- */}
        {isGrant && (
          <div className="space-y-4">
            <div>
              <label htmlFor="ent-email" className="block text-xs font-medium text-gray-700 mb-1">
                Buyer Email <span className="text-red-500">*</span>
              </label>
              <input
                id="ent-email"
                type="email"
                placeholder="user@example.com"
                value={grant.email}
                onChange={(e) => setGrant((s) => ({ ...s, email: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
            <div>
              <label htmlFor="ent-plan-slug" className="block text-xs font-medium text-gray-700 mb-1">
                Plan Slug <span className="text-red-500">*</span>
              </label>
              <input
                id="ent-plan-slug"
                type="text"
                placeholder="e.g. pro, team, unlimited"
                value={grant.plan_slug}
                onChange={(e) => setGrant((s) => ({ ...s, plan_slug: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
            <div>
              <label htmlFor="ent-seats" className="block text-xs font-medium text-gray-700 mb-1">
                Seats
              </label>
              <input
                id="ent-seats"
                type="number"
                min="1"
                step="1"
                value={grant.seats}
                onChange={(e) => setGrant((s) => ({ ...s, seats: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
            <div>
              <label htmlFor="ent-source" className="block text-xs font-medium text-gray-700 mb-1">
                Source
              </label>
              <select
                id="ent-source"
                value={grant.source}
                onChange={(e) => setGrant((s) => ({ ...s, source: e.target.value as 'admin' | 'promo' }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              >
                <option value="admin">admin</option>
                <option value="promo">promo</option>
              </select>
            </div>
          </div>
        )}

        {/* --- Update form --- */}
        {!isGrant && (
          <div className="space-y-4">
            <p className="text-xs text-gray-500 font-mono break-all">
              ref: {update.external_ref}
            </p>
            <div>
              <label htmlFor="upd-plan-slug" className="block text-xs font-medium text-gray-700 mb-1">
                Plan Slug <span className="text-gray-400 font-normal">(blank = no change)</span>
              </label>
              <input
                id="upd-plan-slug"
                type="text"
                placeholder="e.g. pro, team"
                value={update.plan_slug}
                onChange={(e) => setUpdate((s) => ({ ...s, plan_slug: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
            <div>
              <label htmlFor="upd-status" className="block text-xs font-medium text-gray-700 mb-1">
                Status <span className="text-gray-400 font-normal">(blank = no change)</span>
              </label>
              <select
                id="upd-status"
                value={update.status}
                onChange={(e) => setUpdate((s) => ({ ...s, status: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              >
                <option value="">— no change —</option>
                {KNOWN_STATUSES.map((st) => (
                  <option key={st} value={st}>{st}</option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="upd-seats" className="block text-xs font-medium text-gray-700 mb-1">
                Seats <span className="text-gray-400 font-normal">(blank = no change)</span>
              </label>
              <input
                id="upd-seats"
                type="number"
                min="1"
                step="1"
                placeholder="blank = no change"
                value={update.seats}
                onChange={(e) => setUpdate((s) => ({ ...s, seats: e.target.value }))}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
              />
            </div>
          </div>
        )}

        {/* Footer buttons */}
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
            onClick={onSubmit}
            disabled={loading}
            className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50"
          >
            {loading ? 'Saving...' : submitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
