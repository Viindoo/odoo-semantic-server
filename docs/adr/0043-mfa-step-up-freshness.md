# ADR-0043 — MFA Step-Up Freshness: Write Contract + Step-Up Endpoint

**Status:** Accepted
**Date:** 2026-05-29
**Authors:** Engineering team
**Supersedes:** Implied-but-unspecified step-up in ADR-0019 (restore gate) and ADR-0022 (MFA login)
**Related:** ADR-0011 (session auth), ADR-0019 (restore security), ADR-0022 (MFA TOTP),
  ADR-0021 (audit log), ADR-0042 (admin settings)

---

## Context

ADR-0019 (restore upload security) described a `require_admin_with_fresh_mfa` dependency that
checks `session["mfa_verified_at"]` within a 5-minute window. ADR-0022 (MFA TOTP login) defined
the `totp_login` flow but did not specify that it writes `mfa_verified_at`. ADR-0042 (admin
settings) gates destructive settings mutations behind `require_admin_with_fresh_mfa` with a
forward reference to ADR-0022.

**The bug:** `session["mfa_verified_at"]` was read by both `require_admin_with_fresh_mfa` (in
`src/web_ui/auth.py`) and the inline freshness gate in `tenant_settings.py`, but it was **never
written anywhere in the application**. The DB column `active_sessions.mfa_verified_at` (introduced
in migration `m9_005`) also existed but was never populated.

This caused every admin route protected by fresh-MFA to return `403 "Fresh MFA required"`
permanently — including the admin settings page (signup toggle, plans, EE-modules, patterns) and
the restore endpoint.

**Why it went unnoticed:** `is_test_bypass_active()` short-circuits the fresh-MFA gate entirely
when `WEBUI_AUTH_DISABLED=1` + `PYTEST_CURRENT_TEST` are both set (the standard test harness),
so the missing write was invisible to all existing tests. The gate was exercised in production but
the write-side was never implemented.

No prior ADR concretely specified:
1. Which endpoint writes `mfa_verified_at` (session key + DB column).
2. Whether a freshly completed `totp_login` counts as a fresh-MFA event.
3. How an already-logged-in admin re-verifies MFA mid-session (step-up).
4. Whether the freshness window is operator-configurable.
5. What the frontend UX is when a 403 fresh-MFA sentinel is returned.

This ADR fills all five gaps.

---

## Decision

### D1 — `totp_login` writes `mfa_verified_at` (session + DB)

`POST /api/auth/totp/login` (ADR-0022 §5) now writes on success:

```python
request.session["mfa_verified_at"] = time.time()
# AND
UPDATE active_sessions SET mfa_verified_at = NOW()
WHERE session_id = request.session["session_id"]
```

A freshly completed MFA login counts as a fresh-MFA event. The admin is not required to re-verify
immediately after logging in.

### D2 — New step-up endpoint: `POST /api/auth/totp/step-up`

When an already-logged-in admin's MFA freshness has expired, a step-up endpoint allows
re-verification without a full logout-login cycle.

**Contract:**

| Property | Value |
|---|---|
| Method + path | `POST /api/auth/totp/step-up` |
| Auth | Valid session required (caller already authenticated) |
| Body | `{code?: string, backup_code?: string}` — exactly one of TOTP code **or** backup code |
| Rate limit | Same counter as `totp_login`: per-username + per-IP `login_attempts` window |
| On success | Sets `request.session["mfa_verified_at"] = time.time()` + best-effort `UPDATE active_sessions SET mfa_verified_at = NOW()` |
| Audit | `@audit_action("user.mfa.stepup")` — a **distinct** action from `totp_login`'s `user.login.mfa`, so audit queries can separate mid-session step-up from initial MFA login |

**Response contract (actual implementation — authoritative):**

The status codes mirror `totp_login` for consistency (bad factor → `401`, malformed
request → `400`). This supersedes the earlier draft that listed `403 {error:
"invalid_mfa_code"}` / `403 {error: "mfa_not_enrolled"}`.

| Condition | Status | Body |
|---|---|---|
| Success | `200` | `{"ok": true}` |
| Not authenticated (no session) | `401` | `{"error": "not_authenticated"}` |
| User row missing | `404` | `{"error": "user_not_found"}` |
| TOTP not enrolled or `enabled=FALSE` | `400` | `{"error": "totp_not_setup"}` |
| Rate-limited | `429` | `{"error": "Too many failed login attempts; please wait before retrying"}` |
| Invalid TOTP code | `401` | `{"error": "invalid_code"}` |
| Invalid backup code | `401` | `{"error": "invalid_backup_code"}` |
| Neither `code` nor `backup_code` supplied | `400` | `{"error": "code_or_backup_code_required"}` |

The endpoint is exempt from the fresh-MFA gate itself (it IS the step-up mechanism, not a
consumer of it).

### D3 — Shared `_check_mfa_freshness(request)` helper

Both `require_admin_with_fresh_mfa` (FastAPI dependency) and any inline freshness checks (e.g.,
`tenant_settings.py`) use a single helper:

```python
STEP_UP_ERROR_CODE = "mfa_freshness_required"

def _check_mfa_freshness(request: Request) -> None:
    """Raises HTTPException(403) with a STRUCTURED detail if MFA is absent/stale.

    detail = {"error": "mfa_freshness_required", "message": "Fresh MFA required — ..."}
    """
    window = get_mfa_freshness()
    ts = request.session.get("mfa_verified_at")
    if ts is None or (time.time() - float(ts)) > window:
        raise HTTPException(
            status_code=403,
            detail={"error": STEP_UP_ERROR_CODE, "message": "Fresh MFA required — ..."},
        )
```

Note: the test bypass is applied by the *callers* (`require_admin_with_fresh_mfa`,
`_require_tenant_owner_or_admin_with_mfa`), not inside the helper, so the helper stays a
pure check. The freshness window is clamped to the catalogue bounds `[60, 3600]` inside
`get_mfa_freshness()` so a corrupt/zero DB value cannot permanently lock out admin ops.

The 403 detail is a **structured dict**: the frontend keys on the stable
`detail.error == "mfa_freshness_required"` discriminator (machine-readable, reword-proof),
while `detail.message` retains the human `"Fresh MFA required"` sentinel for back-compat and
non-JS clients — see D5.

### D4 — Freshness window is runtime-configurable via `app_settings` (ADR-0042)

The freshness window is a new Tier-1 setting added to the `SETTINGS_CATALOGUE`:

| Key | Default | Min | Max | Category |
|---|---|---|---|---|
| `auth.mfa_freshness_seconds` | `300` | `60` | `3600` | auth |

Getter:

```python
def get_mfa_freshness() -> int:
    """Return the MFA freshness window in seconds (default 300)."""
    return int(get_setting("auth.mfa_freshness_seconds") or MFA_FRESHNESS_SECONDS)
```

The fallback constant `MFA_FRESHNESS_SECONDS = 300` (in `src/web_ui/auth.py`) remains as the
code-level default when `app_settings` is unavailable (e.g., DB unreachable at startup).

This raises the Tier-1 settings count from 15 to **16** (see ADR-0042 §Phase 1 / File Map).

> **Amendment 2026-05-31 (PR #223):** PR #223 adds `support.helpdesk_url` as the 17th non-billing
> Tier-1 entry (`support.*` category). The authoritative count is always `src/settings_registry.py`
> docstring — not this ADR.
>
> **Amendment 2026-06-01 (PR #225):** PR #225 adds `analytics.ga_measurement_id` as the 18th
> non-billing Tier-1 entry (`analytics.*` category). Total catalogue = 29. SSOT unchanged:
> `src/settings_registry.py` docstring.

### D5 — Frontend: sentinel-detect → StepUpMfaModal → retry-once

When a fetch call receives `403` with body `{"detail": {"error": "mfa_freshness_required",
"message": "Fresh MFA required — ..."}}`, the frontend intercepts before surfacing an error
to the user:

1. `withStepUp(action)` wrapper detects the gate via the stable
   `detail.error === "mfa_freshness_required"` code (primary), falling back to a
   `"Fresh MFA required"` substring match on `detail.message`/a plain-string detail for
   back-compat (`STEP_UP_ERROR_CODE` / `FRESH_MFA_SENTINEL` in `site/src/lib/mfaStepUp.ts`).
2. A `StepUpMfaModal` React island renders: "Re-enter your TOTP code to continue."
3. On successful step-up (200 from `POST /api/auth/totp/step-up`), the wrapper retries
   the original action **once**.
4. If the retry also returns 403, the error is surfaced normally (no infinite loop).
5. All admin action islands that trigger fresh-MFA-gated routes are wrapped via `withStepUp`.

The frontend step-up path is **web-UI only** — no MCP tool changes, tool count stays **24**.

### D6 — Write contract is authoritative for `mfa_verified_at`

The complete write contract for `mfa_verified_at` is:

| Writer | Trigger | Session key written | DB column written |
|---|---|---|---|
| `totp_login` (ADR-0022) | Successful TOTP or backup-code login | `session["mfa_verified_at"]` | `active_sessions.mfa_verified_at` |
| `step-up` endpoint (D2) | Successful TOTP or backup-code step-up | `session["mfa_verified_at"]` | `active_sessions.mfa_verified_at` |

No other code path writes this key. Future MFA methods (e.g., WebAuthn) MUST also write both
before being admitted to a fresh-MFA gate.

**Authoritative source for the gate (F6):** the fresh-MFA gate (`_check_mfa_freshness`) reads
ONLY the cookie session `mfa_verified_at`. The `active_sessions.mfa_verified_at` DB column is
written now (best-effort, non-fatal on failure) but is **reserved for future server-side
freshness checks / cross-device session inspection** — it is not consulted by the current gate.
The cookie session is the single authoritative source today. Keeping the DB write in place means
no migration is needed when a server-side check is added; if the best-effort write fails it is
logged at WARNING and does not affect the request.

---

## Consequences

### Positive

- All admin routes gated by `require_admin_with_fresh_mfa` are now reachable (bug fixed).
- The freshness window is operator-tunable (60–3600s) without redeploy via admin settings.
- Audit log records MFA re-verification under a distinct `user.mfa.stepup` action, so audit
  queries can separate mid-session step-up from initial MFA login (`user.login.mfa`).
- Frontend step-up UX eliminates forced logout-login for mid-session freshness expiry.
- Shared `_check_mfa_freshness()` helper removes the duplicated inline gate in `tenant_settings.py`.

### Negative

- `active_sessions.mfa_verified_at` column (migration `m9_005`) now participates in the write
  path — no new migration needed, but the DB column goes from "never written" to "always written
  on MFA event".
- `auth.mfa_freshness_seconds` adds one entry to `SETTINGS_CATALOGUE`; Tier-1 count bumps 15→16.
- All admin action islands must be wrapped in `withStepUp()` — new front-end discipline requirement.

### Risks (mitigations)

1. Admin sets `auth.mfa_freshness_seconds=3600` — session-hijack window widens.
   Mitigation: `max=3600` hard-coded in catalogue; recommended default 300s documented in UI tooltip.
2. Backup code used for step-up consuming single-use codes faster.
   Mitigation: `used_at` enforcement unchanged (ADR-0022 §4); admin can regenerate backup codes.
3. Test bypass masks regressions in write path.
   Mitigation: dedicated non-bypass integration tests for step-up endpoint added alongside fix.

---

## Related ADRs

| ADR | Relationship |
|---|---|
| ADR-0011 | Session auth base — `session_at` TTL, cookie policy |
| ADR-0019 | Introduced `require_admin_with_fresh_mfa`; implied but did not specify the write path |
| ADR-0021 | Audit log decorator reused for step-up event (distinct `user.mfa.stepup` action) |
| ADR-0022 | MFA TOTP enrollment + login; this ADR extends it with the `mfa_verified_at` write contract |
| ADR-0042 | Admin settings; `auth.mfa_freshness_seconds` added as 16th Tier-1 setting |
