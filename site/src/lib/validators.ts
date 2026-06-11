// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * validators — small, composable form-field validators for UI input.
 *
 * Each validator returns `null` when the value is VALID, or a human-readable
 * error string when INVALID. This convention lets call sites do:
 *
 *   const err = otpDigits(code) ?? minLength(12)(pw);
 *   if (err) { setFormError(err); return; }
 *
 * These are client-side guards for fast feedback only. The server remains the
 * single source of truth for every rule — never rely on these for security.
 */

/**
 * FE mirror of the server's common-password blocklist
 * (`src/web_ui/auth.py::_COMMON_PASSWORDS`). This is ONLY a convenience guard
 * for early feedback; the server-side check is the authoritative gate. Kept
 * deliberately small (top ~40) — do not let it drift into a security control.
 */
const COMMON_PASSWORDS: ReadonlySet<string> = new Set([
  'password', 'password1', 'password12', 'password123',
  '123456', '12345678', '123456789', '1234567890',
  'qwerty', 'qwerty123', 'qwertyuiop',
  'abc123', 'abcdefgh', 'letmein', 'welcome', 'welcome1',
  'monkey', 'dragon', 'master', 'sunshine',
  'princess', 'iloveyou', 'admin123', 'admin1234',
  'passw0rd', 'p@ssword', 'p@ssw0rd', 'changeme',
  'newpassword', 'login', 'starwars', 'trustno1',
  'shadow', '111111', '000000', '123123',
  'admin', 'root', 'test1234', 'qwertyuiop123',
]);

/**
 * Build a validator that rejects strings shorter than `min` characters.
 * @returns `(s) => string | null`
 */
export function minLength(min: number): (s: string) => string | null {
  return (s: string): string | null =>
    s.length >= min ? null : `Must be at least ${min} characters.`;
}

/** Both values must be identical (e.g. password confirmation). */
export function confirmMatch(a: string, b: string): string | null {
  return a === b ? null : 'Values do not match.';
}

/** Exactly six digits (TOTP / OTP). */
export function otpDigits(s: string): string | null {
  return /^\d{6}$/.test(s) ? null : 'Enter the 6-digit code.';
}

/** A non-negative integer (e.g. a rate-limit / quota override). */
export function nonNegInt(s: string): string | null {
  return /^\d+$/.test(s.trim()) ? null : 'Must be a non-negative integer.';
}

/** Non-empty after trimming surrounding whitespace. */
export function trimNonEmpty(s: string): string | null {
  return s.trim().length > 0 ? null : 'This field is required.';
}

/** A syntactically plausible email address. */
export function email(s: string): string | null {
  // Pragmatic single-@ check — the server does authoritative validation.
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s.trim())
    ? null
    : 'Enter a valid email address.';
}

/**
 * Reject passwords on the common-password blocklist (case-insensitive).
 * FE mirror only — see {@link COMMON_PASSWORDS}; the server is the SSOT.
 */
export function commonPwBlocklist(s: string): string | null {
  return COMMON_PASSWORDS.has(s.toLowerCase())
    ? 'This password is too common. Choose a less predictable one.'
    : null;
}
