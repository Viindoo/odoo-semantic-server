# ops/offsite-backup-systemd.template/

Template files for the OSM offsite backup sync systemd units. **Not installed — operator fills placeholders and deploys.**

| File | Purpose |
|------|---------|
| `rclone.conf.template` | rclone config skeleton: S3-compatible remote + crypt layer. Fill `@@PROVIDER_ENDPOINT@@`, `@@BUCKET@@`, `@@S3_ACCESS_KEY@@`, `@@S3_SECRET@@`, `@@CRYPT_PASSWORD_OBSCURED@@`. |
| `odoo-semantic-offsite-sync.service.template` | systemd `Type=oneshot` service that runs `rclone sync` + local retention. Fill `@@CRYPT_REMOTE@@` (the `[crypt]` stanza name from rclone.conf). |
| `odoo-semantic-offsite-sync.timer.template` | systemd timer: `OnCalendar=*-*-* 21:00:00 UTC` (= 04:00 ICT). No placeholders — copy as-is. |

For the full deployment procedure — credential bootstrap, dry-run verification, `systemctl enable`, and rollback — see **`docs/deploy/runbooks/offsite-backup-setup.md`**.
