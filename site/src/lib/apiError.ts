// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * apiError — single source of truth for turning a parsed FastAPI error body
 * into a human-readable string.
 *
 * ## Why this exists
 *
 * Before this helper, ~67 call sites manually did
 * `data.detail ?? data.error ?? fallback`. That breaks whenever the backend
 * returns a NON-string `detail`:
 *   - MFA 403:  `{ detail: { error: "mfa_freshness_required", message: "..." } }`
 *   - pydantic 422: `{ detail: [{ loc: ["body","name"], msg: "field required" }] }`
 * Stringifying those naively renders the dreaded `[object Object]` to the user.
 *
 * `extractApiError` is the SSOT replacement. INVARIANT: it ALWAYS returns a
 * string and NEVER returns "[object Object]".
 */

/** First string-valued property of a plain object, or null if none. */
function firstStringValue(obj: Record<string, unknown>): string | null {
  for (const v of Object.values(obj)) {
    if (typeof v === 'string') return v;
  }
  return null;
}

/**
 * Render a pydantic-422 error list (`[{loc, msg, type}, ...]`) into a
 * human-readable string: `"<last loc segment>: <msg>"` per item, joined by
 * "; ". Items without a string `msg` are skipped. Returns null when nothing
 * could be extracted (so the caller falls through to the next strategy).
 */
function renderValidationList(list: unknown[]): string | null {
  const parts: string[] = [];
  for (const item of list) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const msg = rec.msg;
    if (typeof msg !== 'string') continue;
    const loc = rec.loc;
    if (Array.isArray(loc) && loc.length > 0) {
      const last = loc[loc.length - 1];
      parts.push(`${String(last)}: ${msg}`);
    } else {
      parts.push(msg);
    }
  }
  return parts.length > 0 ? parts.join('; ') : null;
}

/**
 * Extract a human-readable error message from a parsed JSON error body.
 *
 * Resolution order (first match wins):
 *   1. `data.detail` is a string            → return it.
 *   2. `data.detail` is an Array (pydantic)  → render `"<loc>: <msg>"; ...`.
 *   3. `data.detail` is an object            → `message ?? error ?? <first string value>`.
 *   4. `data.error` is a string             → return it.
 *   5. `data.message` is a string           → return it.
 *   6. `fallback`.
 *
 * Any object/array branch that cannot extract a string falls through to the
 * next strategy (and ultimately `fallback`) — an object is NEVER stringified.
 *
 * @param data     Parsed response body (any shape, including null/undefined).
 * @param fallback Message returned when no readable error can be extracted.
 */
export function extractApiError(data: unknown, fallback: string): string {
  if (!data || typeof data !== 'object') return fallback;
  const obj = data as Record<string, unknown>;

  const detail = obj.detail;

  // 1. string detail
  if (typeof detail === 'string') return detail;

  // 2. pydantic-422 list detail
  if (Array.isArray(detail)) {
    const rendered = renderValidationList(detail);
    if (rendered !== null) return rendered;
    // else fall through
  } else if (detail && typeof detail === 'object') {
    // 3. object detail (e.g. MFA: { error, message })
    const d = detail as Record<string, unknown>;
    if (typeof d.message === 'string') return d.message;
    if (typeof d.error === 'string') return d.error;
    const first = firstStringValue(d);
    if (first !== null) return first;
    // else fall through
  }

  // 4. top-level error
  if (typeof obj.error === 'string') return obj.error;

  // 5. top-level message
  if (typeof obj.message === 'string') return obj.message;

  // 6. fallback
  return fallback;
}
