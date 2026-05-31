// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT for public-facing contact / support URLs + legal-entity identity.
 *
 *  WI-5/6 will fetch the live helpdesk value from GET /api/site-config (which
 *  reads the `support.helpdesk_url` app-setting). HELPDESK_URL_FALLBACK is the
 *  fallback used when that fetch fails (network error, server cold-start, etc.).
 *
 *  The LEGAL_* constants below are the single source of truth consumed by the
 *  legal pages (terms.astro / privacy.astro / refund.astro). Placeholders use
 *  the `[[NAME]]` convention so they can be grepped and filled before publish.
 */

export const HELPDESK_URL_FALLBACK = 'https://viindoo.com/ticket/team/88';

/** Legal / privacy contact addresses (C-3). */
export const LEGAL_EMAIL = 'legal@viindoo.com';
export const PRIVACY_EMAIL = 'privacy@viindoo.com';
export const SUPPORT_EMAIL = 'support@viindoo.com';
export const SALES_EMAIL = 'sales@viindoo.com';

/**
 * Legal entity identity (C-4). SSOT for the registered operator named across
 * all legal pages.
 */
export const LEGAL_ENTITY = {
  /** Full registered legal name of the operator (English). */
  name: 'Viindoo Technology Joint Stock Company',
  /** Short trading name used in running prose. */
  shortName: 'Viindoo',
  /** Country of incorporation. */
  country: 'Vietnam',
  /** Business / enterprise registration number (MSDN / MST). Issued by the
   *  Department of Planning and Investment of Hai Phong City. */
  registrationNo: '0201994665',
  /** Registered office address (English). */
  registeredAddress: 'Room 820-823, Floor 8, Thanh Dat 3 Building, No. 4 Le Thanh Tong Street, Ngo Quyen Ward, Hai Phong City, Vietnam',
  /** Contact phone number. */
  phone: '+84 225 730 9838',
  /** Merchant of Record (seller of record) for paid subscriptions. */
  merchantOfRecord: 'Polar Software Inc. (polar.sh)',
} as const;

/**
 * Effective date shown on legal pages (C-1). Single string so all three pages
 * stay in sync. ISO 8601 date; use the format helpers below for display.
 */
export const LEGAL_EFFECTIVE_DATE = '2026-06-01';

/** Human-readable effective date in English, e.g. "June 1, 2026". */
export const LEGAL_EFFECTIVE_DATE_EN = 'June 1, 2026';

/**
 * Data Protection Officer / privacy contact point.
 * Also doubles as the PDPL 91/2025 data-rights contact.
 */
export const DPO_CONTACT = 'privacy@viindoo.com';

/** Vietnamese supervisory authority for data protection (A05, MPS). */
export const VN_SUPERVISORY_AUTHORITY =
  'Department of Cyber Security and Hi-Tech Crime Prevention (A05), Ministry of Public Security of Vietnam';

/**
 * Hosting / infrastructure providers (processor list for Section 6 of Privacy Policy).
 * Self-hosted — Viindoo operates its own infrastructure in Vietnam.
 */
export const HOSTING_PROVIDER = 'Viindoo self-hosted infrastructure (Vietnam)';
export const HOSTING_REGION = 'Vietnam';
export const EMAIL_PROVIDER = 'Viindoo-operated mail (@viindoo.com)';

// ---------------------------------------------------------------------------
// Placeholder-aware rendering helpers
// ---------------------------------------------------------------------------
// The `[[NAME]]` placeholders above are intentionally NOT filled with real
// values until legal sign-off (WI-6). Rendering them verbatim would leak raw
// `[[BUSINESS_REG_NO]]`-style tokens into the user-facing legal pages, which
// looks broken. These helpers keep the DRAFT pages presentable WITHOUT filling
// the real data: an unfilled placeholder renders as a graceful "to be confirmed"
// label (or is hidden), and unfilled contact emails fall back to the helpdesk.

/** True when `s` is an unfilled `[[...]]` placeholder token. */
export function isPlaceholder(s: string | null | undefined): boolean {
  return typeof s === 'string' && /^\[\[.*\]\]$/.test(s.trim());
}

/**
 * Render a legal-text value: if it is still an unfilled `[[...]]` placeholder,
 * return a neutral "to be confirmed" label instead of the raw token so DRAFT
 * pages read cleanly. Otherwise return the real value unchanged. The label is
 * returned bare (no wrapping parens) so call sites already inside a parenthetical
 * don't get doubled parens; wrap at the call site if you want emphasis.
 */
export function legalValue(s: string | null | undefined, label = 'to be confirmed'): string {
  return isPlaceholder(s) ? label : (s ?? '');
}

/**
 * Resolve a contact email for logic (null-safe check). Returns null only when
 * the value is empty or an unfilled placeholder. With real email values this
 * always returns the address. Note: use ObfuscatedEmail component for rendering
 * on public pages — do NOT render raw mailto: links from this helper there.
 */
export function contactEmail(s: string | null | undefined): string | null {
  return isPlaceholder(s) ? null : (s || null);
}
