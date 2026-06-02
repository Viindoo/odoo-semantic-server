# ADR-0020 — FERNET Key Delivery and Rotation Procedure

**Status:** Accepted (updated 2026-05-25 — WI-7 hardening)  
**Date:** 2026-05-15  
**Authors:** W-FE stream (M9 security hardening); WI-7 update (M13 FERNET hardening)

---

## Context

### Original M9 findings

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

### WI-7 (M13) additional finding

**F14 — `rotate-fernet` did not cover `totp_secrets`.**  
`totp_secrets.secret_encrypted` is encrypted with the same FERNET_KEY as
`ssh_key_pairs.private_key_encrypted`. Rotating the key without re-encrypting
TOTP secrets would render all enrolled TOTP authenticators unreachable.

**F15 — `--old-key`/`--new-key` CLI flags not yet removed.**  
ADR-0020 (M9) deprecated these flags and promised removal in M10. WI-7
completes the removal — scripts must use `--old-key-env`/`--new-key-env`.

**F16 — Duplicate `_get_fernet()` in two route files.**  
`ssh_keys.py` and `totp.py` each had a private `_get_fernet()` that read
`FERNET_KEY` directly from the environment. Changes to the key-resolution
logic (e.g. adding `CREDENTIALS_DIRECTORY` support) had to be duplicated.
`src/crypto.py` is the single source of truth.

---

## Decision

### Key Delivery

1. **Central getter: `src.crypto.get_fernet_key()` / `get_fernet()`.**  
   All code that needs the FERNET key calls `src.crypto.get_fernet_key()`.
   Resolution order (first wins):
   - `$CREDENTIALS_DIRECTORY/FERNET_KEY` — systemd `LoadCredential` (preferred
     in production: key never touches process environment or cmdline).
   - `$FERNET_KEY` — environment variable (existing deployments continue
     to work without any change).

2. **Startup assertion (F12).**  
   `src/web_ui/__main__.py` checks `ENVIRONMENT` at startup via
   `get_fernet_key()`:
   - `production` → `log.error` + `SystemExit(1)` if key is absent.
   - any other value → `log.warning` only (dev mode, SSH/TOTP features
     disabled).

3. **Env-file permissions check.**  
   An optional `WEBUI_ENV_FILE` env var names the systemd `EnvironmentFile`
   path.  If set, `check_env_file_perms(path)` verifies `(stat.st_mode & 0o077) == 0`
   (owner-read-only, mode 0600) and calls `SystemExit(1)` if not.  This is
   optional — not mandatory for dev environments — but is recommended in
   production.

4. **Recommended systemd setup (via `EnvironmentFile=`, existing deployments).**

   > **Path note (post-ADR-0027):** the `/etc/odoo-semantic/webui.env` shown below predates the
   > system-user layout. The canonical location is now `/home/odoo-semantic/etc/webui.env` (under the
   > service user's `$HOME`). This whole `EnvironmentFile=` approach is in any case superseded by
   > the `LoadCredential` design in item 5 below — FERNET_KEY now lives at `/etc/credstore/FERNET_KEY`.

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

5. **Preferred systemd setup via `LoadCredential` — now the active shipped design (WI-7 holistic cut realized).**

   The key lives at `/etc/credstore/FERNET_KEY` (root:root 0600). Both the
   `odoo-semantic-webui.service` and `odoo-semantic-backup.service` units ship
   with an **active** (not commented-out) `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY`
   directive. The ad-hoc CLI (indexer, `rotate-fernet`, `restore`) receives the key via
   the `osm-fernet-run` wrapper (`systemd-run -p LoadCredential=`), which spawns a
   transient unit as `odoo-semantic` with the credstore credential injected — NOT by
   widening credstore permissions and NOT by keeping the key in the process environment.
   `FERNET_KEY` has been removed from `.env` / `webui.env`.

   ```bash
   # One-time provision (MUST happen BEFORE enabling the webui or backup units):
   sudo install -d -m 0700 -o root -g root /etc/credstore
   # Reuse the existing key (do NOT generate a new one — existing SSH/TOTP secrets
   # are encrypted under the current key and must remain decryptable):
   echo "<current-base64-fernet-key>" | sudo tee /etc/credstore/FERNET_KEY > /dev/null
   sudo chmod 0600 /etc/credstore/FERNET_KEY
   sudo chown root:root /etc/credstore/FERNET_KEY
   ```

   ```ini
   # /etc/systemd/system/odoo-semantic-webui.service  (shipped — active directive)
   [Service]
   LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY
   ExecStart=...
   ```

   ```bash
   # CLI access (indexer / rotate-fernet / restore) — via osm-fernet-run wrapper:
   sudo osm-fernet-run /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
       -m src.cli index --profile ...
   # For rotate-fernet (two keys needed — pass via env):
   sudo OLD_FERNET_KEY=<old> NEW_FERNET_KEY=<new> osm-fernet-run \
       /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.cli rotate-fernet
   ```

   The `src.crypto.get_fernet_key()` getter reads
   `$CREDENTIALS_DIRECTORY/FERNET_KEY` when `CREDENTIALS_DIRECTORY` is set
   by systemd (works identically on Ubuntu 24.04 / systemd 255 and 26.04 / 259).

   > **⚠️ Hard-fail — still applies:** `LoadCredential` with a missing source file
   > causes systemd to refuse to start the unit (exit code 243/CREDENTIALS).
   > This is **not** a soft fallback to `EnvironmentFile=`. Provision
   > `/etc/credstore/FERNET_KEY` **before** enabling or starting the webui/backup
   > units. Operators on env-only deployments may comment the `LoadCredential=` line
   > in a drop-in override (see install-runbook.md).
   >
   > **`$FERNET_KEY` env fallback preserved for dev/non-systemd:** `src.crypto`
   > still honors `$FERNET_KEY` as a fallback when `CREDENTIALS_DIRECTORY` is not
   > set — local dev and non-systemd operators are unaffected.

6. **CLI key delivery via `osm-fernet-run` wrapper (WI-7 holistic cut — RESOLVED).**  
   The holistic cut is now realized: `src/cli.py` (indexer + `rotate-fernet` + `restore`)
   receives the FERNET key via the `osm-fernet-run` wrapper (`docs/deploy/osm-fernet-run`,
   install to `/usr/local/bin/`). The wrapper uses `systemd-run -p LoadCredential=` to
   spawn a transient unit as `odoo-semantic` with `CREDENTIALS_DIRECTORY` set — the key
   is sourced from `/etc/credstore/FERNET_KEY` and never appears in the process environment
   of the calling shell. FERNET_KEY has been removed from `.env` / `webui.env`.

   The earlier "zero net hardening / commented out" caveat is **RESOLVED**: the shipped
   units now carry active `LoadCredential=` directives, and the CLI delivery gap is closed
   by `osm-fernet-run`. The `--old-key` / `--new-key` flags are **removed** (deprecated
   in M9, removed in WI-7); scripts must use `--old-key-env` / `--new-key-env` (default:
   `OLD_FERNET_KEY` / `NEW_FERNET_KEY`).

   Recommended CLI invocations via wrapper:

   ```bash
   # Indexer:
   sudo osm-fernet-run /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
     -m src.cli index --profile ...

   # rotate-fernet (supply both keys via env vars, then via wrapper):
   sudo OLD_FERNET_KEY=<old> NEW_FERNET_KEY=<new> osm-fernet-run \
     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.cli rotate-fernet

   # With custom env var names:
   export MY_OLD_KEY=<old>
   export MY_NEW_KEY=<new>
   sudo osm-fernet-run \
     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.cli rotate-fernet \
     --old-key-env MY_OLD_KEY --new-key-env MY_NEW_KEY
   ```

### Rotation Atomicity (extended — WI-7)

The rotation transaction uses explicit `BEGIN` / `COMMIT` / `ROLLBACK`.
WI-7 extends the transaction to cover **both** `ssh_key_pairs` and
`totp_secrets`:

1. Lock all `ssh_key_pairs` rows with `FOR UPDATE`.
2. Lock all `totp_secrets` rows with `FOR UPDATE` (if table exists).
3. Attempt to decrypt each row with the old key across both tables; collect
   failures.
4. If **any** row in **either** table fails → `conn.rollback()` +
   `SystemExit(2)`. No row is changed.
5. If **all** rows succeed → write one `key_rotation_log` audit row with
   `row_count = ssh_count + totp_count`, then `conn.commit()`.

`totp_secrets` table existence is checked dynamically (graceful skip for
deployments that pre-date M9 MFA).

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

3. **Run rotation** using env vars (--old-key/--new-key flags are REMOVED):
   ```bash
   OLD_FERNET_KEY=<current-production-key> \
   NEW_FERNET_KEY=<newly-generated-key> \
     python -m src.cli rotate-fernet
   ```
   On success you will see:
   `Rotated N SSH key(s) + M TOTP secret(s). Total: N+M row(s).`

4. **Update key delivery** with the new key (choose one):

   **Option A — EnvironmentFile (existing deployments):**
   ```bash
   # Edit /etc/odoo-semantic/webui.env — replace FERNET_KEY value
   systemctl restart odoo-semantic-webui
   ```

   **Option B — LoadCredential (preferred for new deployments):**
   ```bash
   echo "<new-key>" | sudo tee /etc/credstore/FERNET_KEY > /dev/null
   sudo chmod 0600 /etc/credstore/FERNET_KEY
   systemctl restart odoo-semantic-webui
   ```

   > **CLI note:** `src/cli.py` (including `rotate-fernet`) reads FERNET_KEY
   > from the environment / `.env` file, not from the credstore. After updating
   > the credstore and restarting the webui service, also update `FERNET_KEY`
   > in the `.env` / `webui.env` file used by CLI invocations, then re-run
   > the rotation command pointing at the new key.

5. **Verify** the service starts without error:
   ```bash
   journalctl -u odoo-semantic-webui -n 20
   ```

6. **Archive old key** in a secrets manager with a 24-hour retention window
   (in case of emergency rollback).  After 24h, delete the old key from all
   records.

---

## Consequences

- **Dev mode:** running without `FERNET_KEY` logs a warning. SSH key and TOTP
  features will raise errors at runtime.  Set `FERNET_KEY` to any valid Fernet
  key when testing SSH clone or MFA features locally.
- **Rotation safety:** partial rotations are impossible; the database is always
  fully encrypted under one key or the other (both `ssh_key_pairs` and
  `totp_secrets` atomically).
- **Audit trail:** every rotation is permanently recorded in `key_rotation_log`
  with `row_count = ssh_rows + totp_rows`.
- **WI-7 breaking change:** `--old-key` / `--new-key` CLI flags are **removed**.
  Scripts that used these flags must switch to `--old-key-env` / `--new-key-env`
  (or the default `OLD_FERNET_KEY` / `NEW_FERNET_KEY` env vars).
- **WI-7 holistic cut shipped:** The shipped units now carry active `LoadCredential=`
  directives (not commented-out). Operators on env-only deployments may comment the
  `LoadCredential=` line via a drop-in override; `src.crypto` honors `$FERNET_KEY` env
  as a fallback for dev / non-systemd contexts. A missing `/etc/credstore/FERNET_KEY`
  still hard-fails the unit at status=243/CREDENTIALS (NOT a soft fallback) — provision
  the credstore source **before** enabling units. CLI delivery gap is closed by
  `osm-fernet-run` (see §Decision 6 above); FERNET_KEY has been removed from `.env`.
