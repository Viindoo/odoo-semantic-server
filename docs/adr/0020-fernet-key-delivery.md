# ADR-0020 — FERNET Key Delivery and Rotation Procedure

**Status:** Accepted  
**Date:** 2026-05-15  
**Authors:** W-FE stream (M9 security hardening)

---

## Context

Two findings from the M9 security audit drive this ADR:

**F12 — Production fail-fast missing.**  
`src/web_ui/__main__.py` previously only logged a `WARNING` when `FERNET_KEY`
was unset.  In production this silently disables SSH key storage without
alerting the operator.  Any attempt to read or write a key then fails at
runtime with a cryptic error rather than refusing to start cleanly.

**F13 — CLI flag leaks key via `/proc/<pid>/cmdline`.**  
The original `rotate-fernet` sub-command used `--old-key OLD --new-key NEW`.
On Linux, every process argument is visible to any user who can read
`/proc/<pid>/cmdline`.  Passing a 44-byte Fernet key as a CLI flag therefore
exposes it to any co-resident process running as the same UID, to audit logs
that record full command lines, and to shell history.

---

## Decision

### Key Delivery

1. **Startup assertion (F12).**  
   `src/web_ui/__main__.py` checks `ENVIRONMENT` at startup:
   - `production` → `log.error` + `SystemExit(1)` if `FERNET_KEY` unset.
   - any other value → `log.warning` only (dev mode, SSH features disabled).

2. **Env-file permissions check.**  
   An optional `WEBUI_ENV_FILE` env var names the systemd `EnvironmentFile`
   path.  If set, `check_env_file_perms(path)` verifies `(stat.st_mode & 0o077) == 0`
   (owner-read-only, mode 0600) and calls `SystemExit(1)` if not.  This is
   optional — not mandatory for dev environments — but is recommended in
   production.

3. **Recommended systemd setup.**

   ```ini
   # /etc/odoo-semantic/webui.env  (chmod 0600, owned by service user)
   FERNET_KEY=<base64-encoded-key>
   ENVIRONMENT=production
   WEBUI_ENV_FILE=/etc/odoo-semantic/webui.env
   ```

   ```ini
   # /etc/systemd/system/odoo-semantic-webui.service
   [Service]
   EnvironmentFile=/etc/odoo-semantic/webui.env
   ExecStart=...
   ```

   The `EnvironmentFile` directive injects variables into the process
   environment without exposing them on the command line.  The file must be
   mode `0600` and owned by the service account.

4. **CLI key delivery via env var (F13).**  
   `rotate-fernet` now uses `--old-key-env` / `--new-key-env` (default:
   `OLD_FERNET_KEY` / `NEW_FERNET_KEY`) to name the environment variables
   that hold the keys.  The flags `--old-key` / `--new-key` remain for
   backward compatibility but emit a deprecation warning and will be removed
   in M10.

   Recommended invocation:

   ```bash
   OLD_FERNET_KEY=<old> NEW_FERNET_KEY=<new> \
     python -m src.cli rotate-fernet
   ```

   Or with custom env var names:

   ```bash
   export MY_OLD_KEY=<old>
   export MY_NEW_KEY=<new>
   python -m src.cli rotate-fernet \
     --old-key-env MY_OLD_KEY --new-key-env MY_NEW_KEY
   ```

### Rotation Atomicity

The rotation transaction uses explicit `BEGIN` / `COMMIT` / `ROLLBACK` (not
the psycopg2 `with conn:` context manager) to enable a custom abort path:

1. Lock all `ssh_key_pairs` rows with `FOR UPDATE`.
2. Attempt to decrypt each row with the old key; collect failures.
3. If **any** row fails → `conn.rollback()` + `SystemExit(2)`. No row is changed.
4. If **all** rows succeed → write one `key_rotation_log` audit row, then `conn.commit()`.

This guarantees the database is never in a half-rotated state.

### Audit Table

`key_rotation_log` stores one row per successful rotation:

| Column | Type | Notes |
|--------|------|-------|
| `id` | `BIGSERIAL` | Auto-increment PK |
| `rotated_at` | `TIMESTAMPTZ` | Server time of rotation |
| `actor` | `TEXT` | `$USER` / `$LOGNAME` of CLI operator |
| `row_count` | `INTEGER` | Number of re-encrypted rows |
| `old_key_id` | `TEXT` | SHA-256 fingerprint of old key (first 8 bytes) |
| `new_key_id` | `TEXT` | SHA-256 fingerprint of new key (first 8 bytes) |

Fingerprints are 16 hex characters — sufficient to identify which key
generation was involved without storing or revealing key material.

---

## Rotation Procedure

Perform this procedure when rotating the FERNET key (e.g., scheduled annual
rotation, suspected compromise, or operator departure).

1. **Backup database** before starting:
   ```bash
   python -m src.cli backup --output pre-rotation-$(date +%Y%m%d).sql
   ```

2. **Generate new key** and store both old and new in the env file:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. **Run rotation** using env vars (never CLI flags):
   ```bash
   OLD_FERNET_KEY=<current-production-key> \
   NEW_FERNET_KEY=<newly-generated-key> \
     python -m src.cli rotate-fernet
   ```
   On success you will see `Rotated N key(s).`

4. **Update systemd env file** with the new key:
   ```bash
   # Edit /etc/odoo-semantic/webui.env — replace FERNET_KEY value
   systemctl restart odoo-semantic-webui
   ```

5. **Verify** the service starts without error:
   ```bash
   journalctl -u odoo-semantic-webui -n 20
   ```

6. **Archive old key** in a secrets manager with a 24-hour retention window
   (in case of emergency rollback).  After 24h, delete the old key from all
   records.

---

## Consequences

- **Dev mode:** running without `FERNET_KEY` now logs a warning. SSH key
  storage and retrieval will raise errors at runtime.  Set `FERNET_KEY` to any
  valid Fernet key when testing SSH clone features locally.
- **Rotation safety:** partial rotations are impossible; the database is always
  fully encrypted under one key or the other.
- **Audit trail:** every rotation is permanently recorded in `key_rotation_log`.
- **M10 breaking change:** `--old-key` / `--new-key` CLI flags will be removed.
  Scripts must migrate to env-var delivery before M10.
