// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * TotpEnrollment — React island for TOTP enrollment flow (M9 W-MF)
 *
 * Steps:
 *   1. POST /api/auth/totp/setup  → show QR code + manual secret entry
 *   2. Enter 6-digit code → POST /api/auth/totp/verify → show backup codes
 *   3. "I saved these" checkbox → success message
 */

import { useState, useCallback } from 'react';

type Step = 'idle' | 'scanning' | 'verifying' | 'backup_codes' | 'done' | 'error';

interface SetupData {
  secret: string;
  provisioning_uri: string;
  qr_png_base64: string;
}

export default function TotpEnrollment() {
  const [step, setStep] = useState<Step>('idle');
  const [setupData, setSetupData] = useState<SetupData | null>(null);
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [savedConfirmed, setSavedConfirmed] = useState(false);
  const [showSecret, setShowSecret] = useState(false);

  // Step 1: Start enrollment
  const handleSetup = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/totp/setup', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? 'Setup failed. Please try again.');
        setStep('error');
        return;
      }
      setSetupData(data);
      setStep('scanning');
    } catch {
      setError('Network error. Please try again.');
      setStep('error');
    } finally {
      setLoading(false);
    }
  }, []);

  // Step 2: Verify code
  const handleVerify = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/auth/totp/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code.trim() }),
      });
      const data = await res.json();
      if (!res.ok) {
        const msg =
          data.error === 'invalid_code'
            ? 'Invalid code. Check your authenticator app and try again.'
            : data.error === 'totp_not_setup'
            ? 'Session expired. Please start over.'
            : data.error ?? 'Verification failed.';
        setError(msg);
        return;
      }
      setBackupCodes(data.backup_codes ?? []);
      setStep('backup_codes');
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setLoading(false);
    }
  }, [code]);

  // Step 3: Confirm backup codes saved
  const handleDone = useCallback(() => {
    setStep('done');
  }, []);

  // Copy backup codes to clipboard
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(backupCodes.join('\n'));
    } catch {
      // Clipboard not available — codes are shown on screen
    }
  }, [backupCodes]);

  // Reload page to reflect new TOTP status
  const handleReload = useCallback(() => {
    window.location.reload();
  }, []);

  if (step === 'done') {
    return (
      <div
        data-testid="totp-success"
        className="bg-green-50 border border-green-200 rounded-xl px-5 py-4"
      >
        <p className="text-green-800 font-semibold text-sm">
          Two-factor authentication is now enabled.
        </p>
        <p className="text-green-700 text-sm mt-1">
          You will be asked for a code from your authenticator app on your next login.
        </p>
        <button
          onClick={handleReload}
          className="mt-3 text-sm bg-green-700 text-white px-4 py-2 rounded-lg hover:bg-green-800 transition-colors"
        >
          Refresh page
        </button>
      </div>
    );
  }

  if (step === 'backup_codes') {
    return (
      <div data-testid="backup-codes-step" className="space-y-4 max-w-sm">
        <div className="bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
          <p className="text-amber-800 font-semibold text-sm">Save these backup codes</p>
          <p className="text-amber-700 text-sm mt-1">
            Each code can be used once if you lose access to your authenticator app.
            They will not be shown again.
          </p>
        </div>

        <div
          data-testid="backup-codes-list"
          className="bg-gray-900 text-green-400 font-mono text-sm rounded-xl p-4 grid grid-cols-2 gap-1"
        >
          {backupCodes.map((c) => (
            <span key={c} className="tracking-widest">{c}</span>
          ))}
        </div>

        <button
          data-testid="copy-backup-codes-btn"
          onClick={handleCopy}
          className="w-full text-sm border border-gray-300 rounded-lg py-2 hover:bg-gray-50 transition-colors"
        >
          Copy codes to clipboard
        </button>

        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            data-testid="saved-codes-checkbox"
            checked={savedConfirmed}
            onChange={(e) => setSavedConfirmed(e.target.checked)}
            className="accent-viindoo-primary w-4 h-4"
          />
          <span className="text-sm text-gray-700">I have saved these backup codes securely</span>
        </label>

        <button
          data-testid="done-backup-codes-btn"
          onClick={handleDone}
          disabled={!savedConfirmed}
          className="w-full bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 font-medium py-2 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
        >
          I'm done — enable 2FA
        </button>
      </div>
    );
  }

  if (step === 'scanning') {
    return (
      <div data-testid="totp-scan-step" className="space-y-4 max-w-sm">
        <p className="text-sm text-gray-700">
          Scan this QR code with your authenticator app (Google Authenticator, Authy, etc.):
        </p>

        {setupData && (
          <div className="flex flex-col items-center gap-3">
            <img
              data-testid="totp-qr-code"
              src={`data:image/png;base64,${setupData.qr_png_base64}`}
              alt="TOTP QR code"
              className="w-48 h-48 border border-gray-200 rounded-xl"
            />

            <button
              onClick={() => setShowSecret(!showSecret)}
              className="text-xs text-gray-500 hover:text-gray-700 underline"
            >
              {showSecret ? 'Hide' : 'Show'} manual entry key
            </button>

            {showSecret && (
              <div
                data-testid="totp-manual-secret"
                className="bg-gray-100 rounded-lg px-3 py-2 font-mono text-xs text-gray-800 text-center tracking-widest break-all"
              >
                {setupData.secret}
              </div>
            )}
          </div>
        )}

        <form onSubmit={handleVerify} className="space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              6-digit verification code
            </label>
            <input
              type="text"
              inputMode="numeric"
              pattern="[0-9]{6}"
              maxLength={6}
              required
              data-testid="totp-code-input"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-viindoo-primary font-mono text-center text-lg tracking-widest"
              placeholder="000000"
            />
          </div>

          {error && (
            <p
              data-testid="totp-verify-error"
              className="text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg border border-red-200"
            >
              {error}
            </p>
          )}

          <button
            type="submit"
            data-testid="totp-verify-btn"
            disabled={loading || code.length !== 6}
            className="w-full bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 font-medium py-2.5 rounded-lg transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {loading ? 'Verifying…' : 'Verify code'}
          </button>
        </form>
      </div>
    );
  }

  // Initial idle state — show enroll button
  return (
    <div data-testid="totp-idle-step">
      {error && (
        <p
          data-testid="totp-setup-error"
          className="mb-3 text-sm text-red-600 bg-red-50 px-3 py-2 rounded-lg border border-red-200"
        >
          {error}
        </p>
      )}
      <button
        data-testid="enroll-totp-btn"
        onClick={handleSetup}
        disabled={loading}
        className="bg-viindoo-primary hover:bg-viindoo-primary-bright text-viindoo-bg-0 font-medium py-2 px-5 rounded-lg transition-colors text-sm disabled:opacity-50"
      >
        {loading ? 'Setting up…' : 'Enable Two-Factor Authentication'}
      </button>
    </div>
  );
}
