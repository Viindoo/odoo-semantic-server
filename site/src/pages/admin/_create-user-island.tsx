// SPDX-License-Identifier: AGPL-3.0-or-later
// Create User React island — admin-only (W3 B)
// Modal form: username, email, is_admin, optional password.
// Displays temp_password once if the API returns it.
import { useState, useEffect } from 'react';
import { submitJson } from '../../lib/apiClient';
import { flash } from '../../lib/flash';

export default function CreateUserIsland() {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [isAdmin, setIsAdmin] = useState(false);
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // Listen for the trigger button outside this island
  useEffect(() => {
    const btn = document.getElementById('btn-open-create-user');
    if (!btn) return;
    const handler = () => setOpen(true);
    btn.addEventListener('click', handler);
    return () => btn.removeEventListener('click', handler);
  }, []);

  function reset() {
    setUsername('');
    setEmail('');
    setIsAdmin(false);
    setPassword('');
    setFormError(null);
    setTempPassword(null);
  }

  function handleClose() {
    setOpen(false);
    reset();
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setLoading(true);
    setTempPassword(null);

    const body: Record<string, unknown> = { username, is_admin: isAdmin };
    if (email.trim()) body.email = email.trim();
    if (password.trim()) body.password = password.trim();

    try {
      const r = await submitJson<Record<string, unknown>>('/api/admin/users', {
        method: 'POST',
        body,
      });
      if (r.ok) {
        if (r.data.temp_password) {
          // Show temp password — once only
          setTempPassword(String(r.data.temp_password));
        } else {
          flash(`User "${username}" created.`);
          handleClose();
          setTimeout(() => location.reload(), 800);
        }
      } else {
        setFormError(r.error!);
      }
    } catch (err) {
      setFormError(String(err));
    } finally {
      setLoading(false);
    }
  }

  function handleTempPasswordClose() {
    flash(`User "${username}" created. Give them the temp password shown.`);
    handleClose();
    setTimeout(() => location.reload(), 800);
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md mx-4 p-6">
        {tempPassword ? (
          /* Temp-password one-time reveal */
          <div>
            <h2 className="text-lg font-bold text-gray-900 mb-2">User Created</h2>
            <p className="text-sm text-gray-600 mb-4">
              User <strong>{username}</strong> was created with a temporary password.
              Give it to them now — it will not be shown again.
            </p>
            <div className="bg-yellow-50 border border-yellow-300 rounded-xl px-4 py-3 font-mono text-sm text-yellow-900 mb-4 select-all break-all">
              {tempPassword}
            </div>
            <button
              onClick={handleTempPasswordClose}
              className="w-full py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 font-medium text-sm hover:opacity-90"
            >
              Done — I have copied the password
            </button>
          </div>
        ) : (
          /* Create user form */
          <form onSubmit={handleSubmit}>
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-lg font-bold text-gray-900">Create User</h2>
              <button type="button" onClick={handleClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">
                &times;
              </button>
            </div>

            {formError && (
              <div className="mb-4 bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded-lg text-sm">
                {formError}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <label htmlFor="cu-username" className="block text-xs font-medium text-gray-600 mb-1">
                  Username <span className="text-red-500">*</span>
                </label>
                <input
                  id="cu-username"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  placeholder="e.g. john_doe"
                />
              </div>
              <div>
                <label htmlFor="cu-email" className="block text-xs font-medium text-gray-600 mb-1">
                  Email (optional)
                </label>
                <input
                  id="cu-email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  placeholder="user@example.com"
                />
              </div>
              <div>
                <label htmlFor="cu-password" className="block text-xs font-medium text-gray-600 mb-1">
                  Password (leave blank for auto-generated temp password)
                </label>
                <input
                  id="cu-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 bg-white focus:outline-none focus:ring-2 focus:ring-viindoo-primary-deep"
                  placeholder="Leave blank to auto-generate"
                />
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="cu-is-admin"
                  type="checkbox"
                  checked={isAdmin}
                  onChange={(e) => setIsAdmin(e.target.checked)}
                  className="rounded"
                />
                <label htmlFor="cu-is-admin" className="text-sm text-gray-700">
                  Grant admin privileges
                </label>
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
                type="submit"
                disabled={loading || !username.trim()}
                className="flex-1 py-2 rounded-xl bg-viindoo-primary text-viindoo-bg-0 text-sm font-medium hover:opacity-90 disabled:opacity-50"
              >
                {loading ? 'Creating...' : 'Create User'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
