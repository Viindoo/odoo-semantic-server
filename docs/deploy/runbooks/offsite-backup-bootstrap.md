# Offsite Backup Bootstrap Runbook

> First-time setup of rclone + S3-compatible offsite sync with client-side
> AES-256 encryption via rclone crypt, wired into a systemd timer that fires
> daily at 04:00 ICT (after the 03:00 nightly bundle and 03:30 reindex).
> Eliminates the single-point-of-failure of local-only backups. ADR-0018, ADR-0020.

---

## Nguyên Lý

The nightly backup service (`odoo-semantic-backup.service`) writes a bundle
(`postgres.sql` + `neo4j.cypher` + `manifest.json`) to
`/var/backups/odoo-semantic/`. If the production server is lost (disk failure,
fire, accidental wipe), that bundle is gone with it. Offsite sync pushes each
bundle to an S3-compatible bucket *before* the local 7-day retention window
expires, providing an independent recovery anchor.

**Client-side encryption (`rclone crypt`):** the Postgres plain-SQL dump and
Neo4j cypher export never reach the provider unencrypted. AES-256-CTR +
Poly1305-MAC is applied by rclone before any byte leaves the host. Provider SSE
is a secondary layer, not the primary trust boundary.

**Provider-agnostic:** the same service + timer unit works with Backblaze B2,
Wasabi, AWS S3, Cloudflare R2, or self-hosted MinIO. Switching providers
requires only a `rclone.conf` edit — no code change.

---

## Provider Matrix

| Provider | Storage cost (~) | Egress fee | S3-compat | Notes |
|---|---|---|---|---|
| **Backblaze B2** | $0.006/GB/mo | $0.01/GB (free up to 3× storage) | Yes | Recommended for early stage — lowest price + S3 API |
| Wasabi | $0.0059/GB/mo | None | Yes | Competitive; minimum 90-day storage policy applies |
| Cloudflare R2 | $0.015/GB/mo | None | Yes | No egress fee; good for frequent restore drills |
| AWS S3 Standard | $0.023/GB/mo | $0.09/GB | Yes | Industry standard; more expensive; use if already on AWS |
| MinIO self-host | Free | Free | Yes | Not strictly offsite if same datacenter; valid for secondary copy |

**Recommendation:** Backblaze B2 for new deployments. At 3 GB/day × 14-day
upstream retention ≈ 42 GB stored → ~$0.25/mo. Set a billing alert at $5/mo.

---

## Preconditions

Before running any steps below, the operator must complete these business
decisions:

1. **Provider chosen** — see Provider Matrix above.
2. **Bucket created** — globally unique name; set lifecycle rule: delete objects
   older than 14 days.
3. **IAM credentials created** — `access_key_id` + `secret_access_key` (or
   equivalent API key). Minimum required permissions: `s3:PutObject`,
   `s3:GetObject`, `s3:ListBucket` on the chosen bucket prefix. Write-only-to-
   prefix policy reduces blast radius if creds leak.
4. **Crypt passwords decided** — two independent passwords (`password` and
   `password2`) for rclone crypt HMAC. Generated at bootstrap (see Step 3).
   **Back these up offline before proceeding.** Loss = permanent loss of all
   offsite bundles.
5. **Operator has `sudo` / root access** on the production host.

---

## Placeholder Reference

| Placeholder | Default / Example | Note |
|---|---|---|
| `<APP_USER>` | `odoo-semantic` | System user running OSM services (ADR-0027) |
| `<CRYPT_REMOTE>` | `crypt` | Name of the `[crypt]` stanza in rclone.conf |
| `<BUCKET_NAME>` | `osm-backups-prod` | Bucket created by operator at chosen provider |
| `<PROVIDER_ENDPOINT>` | `s3.us-west-000.backblazeb2.com` | Provider S3 endpoint |
| `<OFFSITE_SYNC_SERVICE>` | `odoo-semantic-offsite-sync` | systemd unit base name |

---

## Procedure

### Step 1 — Install rclone

```bash
# Option A: distro package (may lag behind latest)
sudo apt-get install -y rclone

# Option B: latest binary from rclone.org (recommended)
curl https://rclone.org/install.sh | sudo bash

rclone version   # Verify install — expect 1.67+ for robust S3 support
```

### Step 2 — Create credential directories

```bash
# Config directory (rclone.conf lives here)
sudo install -d -m 750 -o root -g <APP_USER> /etc/odoo-semantic/rclone

# Credential files directory (passwords live here — root:odoo-semantic 0640)
sudo install -d -m 750 -o root -g <APP_USER> /etc/odoo-semantic/credentials
```

### Step 3 — Generate and store crypt passwords

**CRITICAL:** Back up both plaintext passwords in an offline password manager or
vault NOW. If these are lost, all offsite bundles become permanently unreadable.
`rclone obscure` produces a base64-like obfuscation that rclone unwraps at
runtime — the credential file IS the secret.

```bash
# Generate and store obscured form of each password
sudo bash -c "rclone obscure \"$(openssl rand -base64 32)\" \
    > /etc/odoo-semantic/credentials/rclone-crypt-pass"
sudo bash -c "rclone obscure \"$(openssl rand -base64 32)\" \
    > /etc/odoo-semantic/credentials/rclone-crypt-pass2"

sudo chmod 640 /etc/odoo-semantic/credentials/rclone-crypt-pass \
               /etc/odoo-semantic/credentials/rclone-crypt-pass2
sudo chown root:<APP_USER> /etc/odoo-semantic/credentials/rclone-crypt-pass \
                            /etc/odoo-semantic/credentials/rclone-crypt-pass2
```

Verify:
```bash
sudo ls -l /etc/odoo-semantic/credentials/
# Expected: two files, mode 640, owner root:<APP_USER>
```

### Step 4 — Write rclone.conf

Create `/etc/odoo-semantic/rclone/rclone.conf` with two stanzas. Passwords are
NOT written here — they are injected at runtime via `LoadCredential=` env vars.

```bash
sudo install -m 640 -o root -g <APP_USER> \
    /dev/stdin /etc/odoo-semantic/rclone/rclone.conf << 'EOF'
# Stanza 1 — S3-compatible backend
[s3-upstream]
type = s3
provider = Other                    # Change to: AWS / Backblaze / Wasabi / Cloudflare
env_auth = false
access_key_id = REPLACE_ME          # From provider IAM console
secret_access_key = REPLACE_ME      # From provider IAM console
endpoint = REPLACE_ME               # e.g. s3.us-west-000.backblazeb2.com
                                    #      s3.wasabisys.com
                                    #      https://minio.internal:9000 (self-hosted)
region =                            # Leave blank for most S3-compat; us-east-1 for AWS
acl = private

# Stanza 2 — crypt remote layered over s3-upstream
[crypt]
type = crypt
remote = s3-upstream:<BUCKET_NAME>
filename_encryption = standard      # Also encrypts file names
directory_name_encryption = true
# password and password2 are NOT written here.
# Injected at runtime by systemd LoadCredential= via env vars:
#   RCLONE_CONFIG_CRYPT_PASSWORD  = contents of rclone-crypt-pass
#   RCLONE_CONFIG_CRYPT_PASSWORD2 = contents of rclone-crypt-pass2
EOF
```

Verify:
```bash
sudo stat -c '%a %U:%G' /etc/odoo-semantic/rclone/rclone.conf
# Expected: 640 root:<APP_USER>
```

### Step 5 — Dry-run connectivity test

Before deploying the systemd unit, verify rclone can reach the provider:

```bash
# Export credentials temporarily for manual test
sudo -u <APP_USER> bash -c '
  export RCLONE_CONFIG=/etc/odoo-semantic/rclone/rclone.conf
  export RCLONE_CONFIG_CRYPT_PASSWORD=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass)
  export RCLONE_CONFIG_CRYPT_PASSWORD2=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass2)
  rclone lsd crypt:
'
```

Expected: either an empty listing (bucket is new) or a list of existing
prefixes. Any authentication or network error indicates a misconfigured
endpoint, access key, or bucket name — fix `rclone.conf` before proceeding.

### Step 6 — Install systemd units

Copy the unit templates from the repo and fill placeholders:

```bash
sudo cp ops/offsite-backup-systemd.template/odoo-semantic-offsite-sync.service \
        /etc/systemd/system/odoo-semantic-offsite-sync.service
sudo cp ops/offsite-backup-systemd.template/odoo-semantic-offsite-sync.timer \
        /etc/systemd/system/odoo-semantic-offsite-sync.timer
```

Edit the service file to replace `<crypt-remote>` with the crypt stanza name
from `rclone.conf` (default: `crypt`):

```bash
sudo sed -i 's|<crypt-remote>|crypt|g' \
    /etc/systemd/system/odoo-semantic-offsite-sync.service
```

Reload and enable the timer (NOT the service directly — the timer controls the
schedule):

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-offsite-sync.timer
```

**Timer schedule:** `OnCalendar=*-*-* 21:00:00 UTC` (= 04:00 ICT / UTC+7).
Runs after the 03:00 ICT nightly bundle (03:00 ICT = 20:00 UTC) and the 03:30
ICT reindex (03:30 ICT = 20:30 UTC) have finished. `RandomizedDelaySec=5min`
spreads the start up to 5 minutes past the hour. `Persistent=true` catches up
on next boot if the system was offline at fire time.

---

## Verification

### After enabling the timer

```bash
systemctl list-timers --all | grep offsite
```

Expected: a line showing `odoo-semantic-offsite-sync.timer` with a next-fire
timestamp ~21:00 UTC.

### After first fire (next day ~04:05 ICT)

```bash
# Check service journal for successful run
journalctl -u odoo-semantic-offsite-sync.service --since "today" | tail -30
# Expected: rclone INFO lines showing files transferred + "Transferred: N files"
# Expected: no ERROR or "exit status" lines

# Verify bundles are visible in the crypt remote
sudo -u <APP_USER> bash -c '
  export RCLONE_CONFIG=/etc/odoo-semantic/rclone/rclone.conf
  export RCLONE_CONFIG_CRYPT_PASSWORD=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass)
  export RCLONE_CONFIG_CRYPT_PASSWORD2=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass2)
  rclone ls crypt:osm/$(hostname)/
'
# Expected: one or more .tar.gz entries with non-zero sizes
```

---

## DR Drill Cadence

Run quarterly. Validates that the offsite copy is reachable and structurally
intact without requiring a full restore.

```bash
# 1. Pull latest bundle (decrypts transparently via crypt remote)
sudo -u <APP_USER> bash -c '
  export RCLONE_CONFIG=/etc/odoo-semantic/rclone/rclone.conf
  export RCLONE_CONFIG_CRYPT_PASSWORD=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass)
  export RCLONE_CONFIG_CRYPT_PASSWORD2=$(cat /etc/odoo-semantic/credentials/rclone-crypt-pass2)
  rclone copy crypt:osm/$(hostname)/ /tmp/osm-restore-drill/ --max-age 24h
'

# 2. Confirm bundle is non-empty
ls -lh /tmp/osm-restore-drill/*.tar.gz

# 3. Check manifest
tar -xzf /tmp/osm-restore-drill/*.tar.gz -C /tmp/osm-restore-drill/ manifest.json
cat /tmp/osm-restore-drill/manifest.json
# Expected: bundle_version, created_at, file checksums

# 4. Check PG dump is valid SQL
tar -xzf /tmp/osm-restore-drill/*.tar.gz -C /tmp/osm-restore-drill/ postgres.sql
head -5 /tmp/osm-restore-drill/postgres.sql
# Expected: -- PostgreSQL database dump

# 5. Cleanup
rm -rf /tmp/osm-restore-drill/
```

For a full in-place restore (disaster recovery), follow
`docs/deploy/disaster-recovery.md` — the drill above only validates the bundle
is reachable and structurally intact.

---

## Rollback

**Disable the timer (stop offsite syncs, data already uploaded is preserved):**

```bash
sudo systemctl disable --now odoo-semantic-offsite-sync.timer
sudo rm /etc/systemd/system/odoo-semantic-offsite-sync.{service,timer}
sudo systemctl daemon-reload
```

Do NOT delete the remote bucket — the offsite anchor may still be the most
recent recovery point. Leave remote data intact until you have confirmed an
alternative backup strategy is in place.

---

## Failure Modes

| Failure | Behaviour | How to detect |
|---|---|---|
| S3 unreachable | rclone exits non-zero; systemd marks unit `failed` | `journalctl -u odoo-semantic-offsite-sync` + `OnFailure=osm-alert@%n.service` fires |
| Crypt password file missing | rclone exits non-zero at config load | Same `OnFailure=` path; journal shows `LoadCredential failed` |
| Disk full — no new bundle | rclone runs, uploads nothing, exits 0 | Silent; disk-full alert from backup unit fires first |
| Bundle partial (backup unit failed) | Partial `.tar.gz` pushed | Backup unit's own `OnFailure=` fires before offsite sync; manifest verify in DR drill catches this |
| 14-day lifecycle purge too aggressive | Operator misconfigured bucket | Quarterly DR drill catches this |

---

## References

- **`ops/offsite-backup-systemd.template/`** — systemd unit templates (created by W1C-2)
- **`docs/adr/0018-backup-contract.md`** — backup bundle contract (tar.gz format, 4-component manifest)
- **`docs/adr/0020-fernet-credstore.md`** — `LoadCredential=` key delivery pattern (reused here for crypt passwords)
- **`docs/adr/0027-system-user-deployment.md`** — canonical paths + `ProtectHome` + `TMPDIR` gotcha
- **`docs/deploy/runbooks/backup-confirm-and-dr-drill.md`** — local backup verification + DR drill
- **rclone crypt docs** — https://rclone.org/crypt/
