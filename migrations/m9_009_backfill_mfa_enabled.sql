-- m9_009: backfill webui_users.mfa_enabled to match totp_secrets.enabled
--
-- One-time idempotent symmetric reconciliation.
-- Safe to re-run (both UPDATEs use WHERE to limit to rows that actually need change).
--
-- Design notes:
--   * totp_secrets.enabled = TRUE indicates user has enrolled and verified TOTP.
--   * webui_users.mfa_enabled must match this state for consistency.
--   * Symmetric: sets TRUE for active TOTP, resets FALSE for absent/disabled TOTP.
--     This handles pre-WI-2 drift where mfa_enabled=TRUE was set without a matching
--     totp_secrets row (e.g. user disabled TOTP before the sync column existed).
--   * Rollback: no-op (one-way data migration, do not reverse).

-- Forward: activate flag for users with enrolled TOTP
UPDATE webui_users SET mfa_enabled = TRUE
WHERE id IN (SELECT user_id FROM totp_secrets WHERE enabled = TRUE);

-- Reverse drift: clear flag for users without active TOTP enrollment
UPDATE webui_users SET mfa_enabled = FALSE
WHERE id NOT IN (SELECT user_id FROM totp_secrets WHERE enabled = TRUE)
  AND mfa_enabled = TRUE;
