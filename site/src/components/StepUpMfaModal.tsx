// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * StepUpMfaModal — global MFA step-up modal host (W4)
 *
 * ## What this does
 * Mounts once (invisible) and listens for `osm:mfa-step-up` CustomEvents
 * fired by `src/lib/mfaStepUp.ts → requestStepUp()`.  When an event arrives
 * it renders a centred modal with a 6-digit TOTP input (and an optional
 * backup-code text input), calls `stepUpVerify(...)`, then resolves the
 * pending promise via `event.detail.resolve(true/false)`.
 *
 * ## Mount location (W5 instruction)
 * Add exactly ONE instance in `site/src/layouts/AdminLayout.astro`,
 * just before the closing `</BaseLayout>` tag:
 *
 *   ```astro
 *   import StepUpMfaModal from '../components/StepUpMfaModal';
 *   ...
 *   <StepUpMfaModal client:load />
 *   ```
 *
 * Do NOT mount it on every page — one instance in the shared layout is
 * sufficient and prevents duplicate event listeners.
 *
 * ## Event contract (for reference)
 * `window` receives:
 *   CustomEvent('osm:mfa-step-up', {
 *     detail: { resolve: (ok: boolean) => void }
 *   })
 * The modal must call `resolve(true)` on success or `resolve(false)` on
 * cancel.  Exactly one call is expected per event.
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { stepUpVerify, claimPendingStepUp } from '../lib/mfaStepUp';

// ---------------------------------------------------------------------------
// Error-code → human message map (mirrors TotpEnrollment patterns)
// ---------------------------------------------------------------------------

function mapError(errorCode: string | undefined, fallback: string): string {
  switch (errorCode) {
    case 'invalid_code':
      return 'Invalid code. Check your authenticator app and try again.';
    case 'invalid_backup_code':
      return 'Invalid backup code. Please try again.';
    case 'code_or_backup_code_required':
      return 'Enter a 6-digit code or a backup code to continue.';
    case 'totp_not_setup':
      return 'Two-factor authentication is not set up on your account. Please enrol at Admin → Security → Two-Factor Authentication.';
    case 'not_authenticated':
      return 'Your session has expired. Please sign in again.';
    case 'code_expired':
      return 'Code has expired. Please wait for the next 30-second window and try again.';
    case 'already_used':
      return 'This code was already used. Wait for the next code.';
    default:
      return fallback || errorCode || 'Verification failed. Please try again.';
  }
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function StepUpMfaModal() {
  // Visibility + pending resolve ref
  const [open, setOpen] = useState(false);
  const resolveRef = useRef<((ok: boolean) => void) | null>(null);

  // Form state
  const [code, setCode] = useState('');
  const [backupCode, setBackupCode] = useState('');
  const [useBackup, setUseBackup] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Listen for the custom event from mfaStepUp.ts
  useEffect(() => {
    function openWith(resolve: (ok: boolean) => void) {
      resolveRef.current = resolve;
      // Reset form state on each new request
      setCode('');
      setBackupCode('');
      setUseBackup(false);
      setError('');
      setLoading(false);
      setOpen(true);
    }
    function handleStepUpRequest(e: Event) {
      const evt = e as CustomEvent<{ resolve: (ok: boolean) => void }>;
      openWith(evt.detail.resolve);
    }

    window.addEventListener('osm:mfa-step-up', handleStepUpRequest);
    // Hydration-race guard: a step-up requested by a sibling client:load island
    // BEFORE this listener attached fired a one-shot event into the void and
    // parked its resolver in mfaStepUp._waiters. Claim it now so the modal still
    // opens instead of leaving the caller's Save hung on "Saving…".
    const pending = claimPendingStepUp();
    if (pending) openWith(pending);
    return () => window.removeEventListener('osm:mfa-step-up', handleStepUpRequest);
  }, []);

  // Resolve the pending promise and close
  const finish = useCallback((ok: boolean) => {
    setOpen(false);
    if (resolveRef.current) {
      resolveRef.current(ok);
      resolveRef.current = null;
    }
  }, []);

  const handleCancel = useCallback(() => {
    finish(false);
  }, [finish]);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setError('');
      setLoading(true);

      const payload = useBackup
        ? { backup_code: backupCode.trim() }
        : { code: code.trim() };

      const result = await stepUpVerify(payload);

      setLoading(false);

      if (result.ok) {
        finish(true);
      } else {
        setError(mapError(result.error, result.error ?? 'Verification failed.'));
      }
    },
    [code, backupCode, useBackup, finish],
  );

  // TOTP input: strip non-digits, max 6
  const handleCodeChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setCode(e.target.value.replace(/\D/g, '').slice(0, 6));
    setError('');
  }, []);

  // Backup code input
  const handleBackupCodeChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setBackupCode(e.target.value);
    setError('');
  }, []);

  // Toggle between TOTP and backup-code mode
  const handleToggleBackup = useCallback(() => {
    setUseBackup((prev) => !prev);
    setCode('');
    setBackupCode('');
    setError('');
  }, []);

  // Keyboard: Escape closes
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') handleCancel();
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, handleCancel]);

  // Nothing to render when idle
  if (!open) return null;

  const canSubmit = useBackup
    ? backupCode.trim().length > 0
    : code.length === 6;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="step-up-title"
      data-testid="step-up-modal"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
    >
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2
              id="step-up-title"
              className="text-lg font-bold text-gray-900"
            >
              Verify your identity
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Your session requires a fresh MFA verification to continue.
            </p>
          </div>
          <button
            type="button"
            onClick={handleCancel}
            aria-label="Cancel"
            className="text-gray-400 hover:text-gray-600 text-xl leading-none p-1"
          >
            &times;
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-4">
          {!useBackup ? (
            /* --- TOTP 6-digit input --- */
            <div>
              <label
                htmlFor="step-up-totp-code"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                6-digit authentication code
              </label>
              <input
                id="step-up-totp-code"
                type="text"
                inputMode="numeric"
                pattern="[0-9]{6}"
                maxLength={6}
                required
                autoFocus
                autoComplete="one-time-code"
                data-testid="step-up-code-input"
                value={code}
                onChange={handleCodeChange}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary font-mono text-center text-lg tracking-widest"
                placeholder="000000"
              />
            </div>
          ) : (
            /* --- Backup code input --- */
            <div>
              <label
                htmlFor="step-up-backup-code"
                className="block text-sm font-medium text-gray-700 mb-1"
              >
                Backup code
              </label>
              <input
                id="step-up-backup-code"
                type="text"
                autoFocus
                autoComplete="off"
                data-testid="step-up-backup-code-input"
                value={backupCode}
                onChange={handleBackupCodeChange}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary font-mono text-center text-lg tracking-widest"
                placeholder="Enter backup code"
              />
            </div>
          )}

          {/* Toggle backup code */}
          <button
            type="button"
            data-testid="step-up-toggle-backup"
            onClick={handleToggleBackup}
            className="text-xs text-gray-500 hover:text-gray-700 underline"
          >
            {useBackup
              ? 'Use authenticator app instead'
              : "Can't access your app? Use a backup code"}
          </button>

          {/* Error block — matches TotpEnrollment styling */}
          {error && (
            <p
              role="alert"
              data-testid="step-up-error"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg border border-red-200"
            >
              {error}
            </p>
          )}

          {/* Buttons */}
          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={handleCancel}
              data-testid="step-up-cancel-btn"
              className="flex-1 py-2 rounded-xl border border-gray-300 text-gray-700 text-sm font-medium hover:bg-gray-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !canSubmit}
              data-testid="step-up-verify-btn"
              className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? 'Verifying…' : 'Verify'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
