# FERNET Key Provisioning Runbook

> Provision `FERNET_KEY` credential for webui + backup services on production.
> ADR-0020: FERNET key delivery + atomic rotation.

---

## Nguyên Lý

### Tại Sao FERNET Cần Thiết

OSM server encrypts sensitive credentials at rest using **Fernet** (symmetric encryption):

- **SSH private key pairs** (`ssh_key_pairs.private_key_encrypted`) — decrypted only at deploy time or CLI rotation.
- **TOTP secrets** (`totp_secrets.secret_encrypted`) — decrypted only when validating MFA codes during login.

Both tables are encrypted by the same `FERNET_KEY` and stored in Postgres. If Postgres is compromised, encrypted rows remain safe as long as the key is not exposed.

### Key Resolution Order

OSM services resolve `FERNET_KEY` in this order (first win):

1. **systemd credential store**: `$CREDENTIALS_DIRECTORY/FERNET_KEY` — **Preferred in production**
   - Systemd manages the file (never expose to process env or cmdline)
   - File lives at root-protected path (`/etc/credstore/FERNET_KEY`, mode 0600, owner root:root)
   - Services receive key via `LoadCredential=` directive (systemd 247+)
   
2. **Environment variable fallback**: `$FERNET_KEY` — for backward compatibility and development
   - Only used if credential store is not available
   - Not recommended in production (key visible to `ps` output + systemd journal + core dumps)

### Services Affected

**Affected (require FERNET_KEY):**
- `<WEBUI_SERVICE>` — FastAPI admin panel (decrypts SSH keys + TOTP for login)
- `<BACKUP_SERVICE>` — Nightly backup oneshot (decrypts SSH keys + TOTP in bundles with `--bundle-passphrase-env`)

**NOT affected:**
- `<MCP_SERVICE>` — MCP server (:8002) does NOT decrypt secrets at runtime; no `LoadCredential` needed

---

## Precondition

Before provisioning:

- Operator has **root access** on the prod host
- **systemd version ≥ 247** (supports `LoadCredential=`); verify:
  ```bash
  systemctl --version | head -1
  # Should show: systemd 247+ (or check with: systemctl show -p Version)
  ```
- `<WEBUI_SERVICE>` and `<BACKUP_SERVICE>` units are **not currently running**, or operator is willing to restart them after provision
- CLI command to generate key is available (see Execute section)

---

## Placeholder Reference — ADR-0027 Canonical Defaults

| Placeholder | Canonical default | Note |
|---|---|---|
| `<CREDSTORE_PATH>` | `/etc/credstore/FERNET_KEY` | Root-protected credential store path |
| `<APP_USER>` | `odoo-semantic` | System user running webui + backup services |
| `<WEBUI_SERVICE>` | `odoo-semantic-webui` | systemd unit name for FastAPI admin |
| `<BACKUP_SERVICE>` | `odoo-semantic-backup` | systemd unit name for nightly backup |
| `<MCP_SERVICE>` | `odoo-semantic-mcp` | systemd unit name for MCP server (NOT affected) |

Operators on non-canonical layouts (e.g., `/opt/` or personal-user deployments) substitute the actual values. Example: if app user is `semantic-bot` and service unit is `semantic-bot-webui`, replace all placeholders accordingly.

---

## Sequence

### 1. Generate FERNET Key (One-Time, Offline Backup First)

**Critical:** Save the generated key in an **offline password manager + vault** before writing to credstore. If the key is lost, all encrypted secrets (SSH keys, TOTP) become unrecoverable.

**Option A:** If CLI command is available:
```bash
# Verify CLI command exists (read-only check, no auth needed)
python3 -m src.cli --help 2>&1 | grep -i fernet || echo "CLI command not found; fall back to Option B"
```

**Option B:** Manual Python generation (always available):
```bash
python3 <<'PYGEN'
from cryptography.fernet import Fernet
key = Fernet.generate_key().decode()
print(key)
PYGEN
```

Output will be a 44-character URL-safe base64 string. Example:
```
Drmhze6EPcv0fN_81Bj-nml33X0Qi5TTJesF5sSi2Fg=
```

**Save this key offline now.**

### 2. Write Credential Store

Create the credential store directory and write the key with strict permissions (root-only read):

```bash
# As root: create directory
sudo install -d -o root -g root -m 0700 "$(dirname '<CREDSTORE_PATH>')"

# Write the key from manual input (paste from offline backup)
echo -n "PASTE_KEY_HERE" | sudo install -o root -g root -m 0600 /dev/stdin '<CREDSTORE_PATH>'

# Or via environment variable if available:
echo -n "$FERNET_KEY" | sudo install -o root -g root -m 0600 /dev/stdin '<CREDSTORE_PATH>'

# Verify file exists and has correct permissions
sudo ls -l '<CREDSTORE_PATH>'
# Expected output:
#   -rw------- 1 root root 44 ... FERNET_KEY
```

**Verify** the file is readable only by root and contains exactly 44 characters:
```bash
sudo wc -c '<CREDSTORE_PATH>'      # → 44 (no newline)
sudo stat -c '%a %U:%G' '<CREDSTORE_PATH>'  # → 600 root:root
```

### 3. Reload systemd + Restart Services

Load the updated `LoadCredential=` directives and restart the affected services:

```bash
# Reload systemd unit definitions
sudo systemctl daemon-reload

# Restart webui service
sudo systemctl restart <WEBUI_SERVICE>

# Restart backup service (oneshot, safe to restart anytime)
sudo systemctl restart <BACKUP_SERVICE>

# Do NOT restart MCP — it does not use FERNET
```

---

## Verify

Run these checks **immediately after** restart to confirm provisioning succeeded:

### 3a. Services Active

```bash
# Both should show "active (running)"
sudo systemctl is-active <WEBUI_SERVICE>
sudo systemctl is-active <BACKUP_SERVICE>

# MCP unaffected (should remain active)
sudo systemctl is-active <MCP_SERVICE>
```

### 3b. Check Logs for FERNET Load Success

```bash
# No FERNET_KEY errors in logs (should be clean or show successful load)
sudo journalctl -u <WEBUI_SERVICE> --since "5 minutes ago" | grep -iE "fernet|credential|FERNET_KEY" || echo "No FERNET logs found (expected)"

sudo journalctl -u <BACKUP_SERVICE> --since "5 minutes ago" | grep -iE "fernet|credential|FERNET_KEY" || echo "No FERNET logs found (expected)"
```

**Expected outcome:** Either no mention of FERNET (service loaded silently) OR debug log like `"FERNET_KEY loaded from CREDENTIALS_DIRECTORY"` — never an error.

### 3c. Smoke Test: Login

If the webui is accessible:

```bash
# Test login at the admin panel
curl -s http://localhost:8003/api/health 2>&1 | head -5
# Should respond with JSON (auth may be required, but no FERNET errors)
```

---

## Rotation

### When to Rotate

Rotate `FERNET_KEY` if:
- Current key is suspected compromised
- Regular security policy requires periodic rotation (e.g., quarterly)
- Operator accidentally exposed the key in logs or via memory dump

### Atomic Rotation Procedure

OSM CLI provides an **atomic rotation** command that re-encrypts both `ssh_key_pairs` and `totp_secrets` in a **single transaction**. If any row fails to decrypt with the old key, the entire operation rolls back (no partial state).

**Prerequisites for rotation:**
- Both old key (currently in credstore) and new key (to be written) available as environment variables
- Postgres must be up and running
- Services can be running (rotation does not lock them out)

**Execute rotation:**

```bash
# Provide old and new keys via environment variables (not command-line to avoid exposure)
export OLD_FERNET_KEY="PASTE_CURRENT_KEY_HERE"
export NEW_FERNET_KEY="PASTE_NEW_KEY_HERE"

# Run rotation (exact command depends on CLI availability; verify with --help)
python3 -m src.cli rotate-fernet --old-key-env OLD_FERNET_KEY --new-key-env NEW_FERNET_KEY

# Typical output on success:
#   Re-encrypting ssh_key_pairs...
#   ssh_key_pairs: 0 rows updated, 0 failures
#   Re-encrypting totp_secrets...
#   totp_secrets: 0 rows updated, 0 failures
#   Audit log entry written; rotation complete.
```

**After successful rotation:**

1. Update credstore with new key:
   ```bash
   echo -n "$NEW_FERNET_KEY" | sudo install -o root -g root -m 0600 /dev/stdin '<CREDSTORE_PATH>'
   ```

2. **Do NOT restart services immediately** (they still have old key loaded in memory). Clear the systemd credential cache by reloading:
   ```bash
   sudo systemctl daemon-reload
   ```

3. Restart services to pick up the new key:
   ```bash
   sudo systemctl restart <WEBUI_SERVICE>
   sudo systemctl restart <BACKUP_SERVICE>
   ```

4. Verify (same checks as section 3b above)

---

## Rollback

### If Provisioning Fails

If after restart, services report `"FERNET_KEY missing"` or `InvalidToken` errors:

**Symptom 1:** File not found or permission denied
```bash
# Check if credstore file exists
sudo test -f '<CREDSTORE_PATH>' && echo "exists" || echo "MISSING — provision again"

# Check permissions
sudo ls -l '<CREDSTORE_PATH>'
# Must be: -rw------- 1 root root ...
```

**Symptom 2:** Key mismatch (InvalidToken during login or backup)
- Credstore key is different from the key used to encrypt existing SSH/TOTP rows
- Recovery: Restore from offline backup (you saved it in step 1)
- Rewrite credstore and restart

**To rollback:**
1. Stop affected services:
   ```bash
   sudo systemctl stop <WEBUI_SERVICE>
   sudo systemctl stop <BACKUP_SERVICE>
   ```

2. Restore the correct key from offline backup:
   ```bash
   # Paste the ORIGINAL key (from offline vault)
   echo -n "ORIGINAL_KEY_FROM_VAULT" | sudo install -o root -g root -m 0600 /dev/stdin '<CREDSTORE_PATH>'
   ```

3. Reload and restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart <WEBUI_SERVICE>
   sudo systemctl restart <BACKUP_SERVICE>
   ```

4. Verify services come up cleanly (no InvalidToken in logs)

**Note:** Rollback is safe because FERNET is read-only encryption at rest — no data is corrupted if the wrong key is loaded. Services simply fail to decrypt until the correct key is restored.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `"FERNET_KEY missing"` in webui/backup logs | `<CREDSTORE_PATH>` file does not exist or `LoadCredential=` directive not loaded | Re-run section 2 (Write Credential Store) + `systemctl daemon-reload` |
| `"InvalidToken"` during login or backup | Credstore key does not match the key used to encrypt existing SSH/TOTP rows | Restore the correct key from offline backup (section Rollback); verify key fingerprint if unsure |
| `systemctl daemon-reload` has no effect; services still fail | systemd version < 247 (does not support `LoadCredential=`) | Verify `systemctl --version`; upgrade systemd or fall back to `EnvironmentFile=` with key in plaintext env file (less secure) |
| `"PermissionError"` when service tries to read credstore | File mode is not 0600, or owner is not root:root | Correct permissions: `sudo chmod 0600 '<CREDSTORE_PATH>'` and `sudo chown root:root '<CREDSTORE_PATH>'` |
| Key rotation command not found | CLI does not expose `rotate-fernet` subcommand | This is a version mismatch; consult ADR-0020 for manual rotation steps, or upgrade OSM |

---

## References

- **`src/crypto.py`** — Canonical key resolution logic (systemd credential store, env fallback)
- **`docs/deploy/odoo-semantic-webui.service`** — `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY` directive
- **`docs/deploy/odoo-semantic-backup.service`** — Same `LoadCredential=` directive
- **`docs/deploy/odoo-semantic-mcp.service`** — Verify NOT present (MCP does not decrypt secrets)
- **`src/cli.py`** — `rotate-fernet` subcommand implementation
- **ADR-0020** — Architecture Decision Record: FERNET key delivery + atomic rotation
- **ADR-0027** — System-user deployment layout (canonical paths + user names)
