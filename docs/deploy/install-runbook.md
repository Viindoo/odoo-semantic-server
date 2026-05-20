# Systemd install runbook

> Idempotent install + upgrade procedure for the OSM systemd units.
> Goal: **routine upstream upgrades do not lose operator customizations**.
> Failure to follow this caused the 2026-05-19 production outage
> documented in [issue #144](https://github.com/Viindoo/odoo-semantic-mcp/issues/144).

---

## When to use this runbook

- **First install** on a new host.
- **Routine upgrade** of unit files when pulling a new release (PR that
  touches `docs/deploy/*.service`).
- **Recovery** after a botched deploy that wiped customizations.
- **Drift audit** before any production deploy (run `make check-systemd-overrides`).

---

## 1. Canonical layout vs operator overrides

The shipped templates in `docs/deploy/*.service` assume the
**ADR-0027 system-user layout**:

| Resource | Canonical path |
|---|---|
| Service account | `odoo-semantic` (system user, group `odoo-semantic`) |
| App directory | `/home/odoo-semantic/odoo-semantic-mcp/` |
| Python venv | `/home/odoo-semantic/.venv/odoo-semantic-mcp/` |
| Config dir | `/home/odoo-semantic/etc/` (mode 700) |
| Main config | `/home/odoo-semantic/etc/odoo-semantic.conf` (mode 600) |
| Web UI env | `/home/odoo-semantic/etc/webui.env` (mode 600) |
| Main `.env` | `/home/odoo-semantic/odoo-semantic-mcp/.env` (mode 600) |
| Repos clone root | `/home/odoo-semantic/repos/` |
| Backup output | `/var/backups/odoo-semantic/` (mode 0750, owned by service user) |
| Backup tmpdir | `/var/tmp` |

If your deployment uses any of these paths exactly, you do **not** need
any drop-in override. The shipped templates work as-is.

If your deployment diverges on any path (legacy `/opt/`, personal-user,
alternate config dir), use **systemd drop-in overrides** at
`/etc/systemd/system/<unit>.service.d/<your-name>.conf`. See
[`docs/deploy/overrides/README.md`](overrides/README.md) for the full
override-merge semantics + ready-to-copy example files.

**Do not edit the shipped unit body in place.** Every upstream upgrade
will silently wipe in-body edits unless you tag every divergence by
hand. Drop-ins are merged automatically by `systemctl daemon-reload`
and survive `cp` / `install` of the upstream template.

---

## 2. Pre-deploy drift audit

Before any deploy that touches systemd unit files, run:

```bash
make check-systemd-overrides
```

This script (`scripts/check-systemd-overrides.sh`) audits each unit in
its `UNITS` list (the 4 main services + `osm-ttl-cleanup.service`;
`osm-alert@.service` and `*.timer` units are intentionally excluded —
no body-customization surface) by comparing the installed body at
`/etc/systemd/system/<unit>.service` against the shipped template at
`docs/deploy/<unit>.service`, ignoring comment-only differences.

The script prints a per-unit status line and a four-line summary. The
status markers are:

- `[✓] <unit>: in sync with shipped template` — canonical install,
  body matches (ignoring comments).
- `[✗] <unit>: BODY DRIFT detected` — canonical install whose body was
  hand-edited in place. The unified diff follows. **Move those edits
  to a drop-in before upgrading.** Counts toward `Body drift` and
  makes the script exit non-zero.
- `[~] <unit>: non-canonical install (User=<x>)` — the installed unit
  was rendered by a non-root `install.sh` run (substituted paths +
  `User=`), so a direct body diff is not meaningful. Body audit is
  skipped; verify your customizations live in a drop-in. Counts toward
  `Non-canonical`.
- `[ ] <unit>: not installed` — no installed body. Counts toward
  `Not installed`.
- `[!] orphan drop-in: ...` — a `<unit>.service.d/` dir exists but the
  unit body is missing. Counts toward `Orphan dirs`.

Summary block (exact counters printed):

```
=== Summary ===
  Body drift:      <n>
  Not installed:   <n>
  Non-canonical:   <n> (body audit skipped — verify drop-ins)
  Orphan dirs:     <n>
```

Exit code is non-zero **only** when `Body drift > 0`. `Not installed`,
`Non-canonical`, and `Orphan dirs` are informational and do not fail
the audit.

Expected on a healthy canonical-layout host (root install): all
audited units `[✓] in sync`, `Body drift: 0`.

Expected on a healthy non-canonical-layout host (dev / substituted
install): units show `[~] non-canonical`, `Body drift: 0`,
`Non-canonical: <count>`; verify a drop-in `*.conf` exists per
customized unit.

---

## 3. First-install procedure (canonical layout)

```bash
# 1. Pull the repo + install Python deps (creates venv at canonical path)
cd /home/odoo-semantic/odoo-semantic-mcp  # already cloned
make install

# 2. Install all systemd units (idempotent — uses install.sh logic)
sudo bash install.sh --systemd

# Or manually:
sudo cp docs/deploy/odoo-semantic-mcp.service       /etc/systemd/system/
sudo cp docs/deploy/odoo-semantic-webui.service     /etc/systemd/system/
sudo cp docs/deploy/odoo-semantic-astro.service     /etc/systemd/system/
sudo cp docs/deploy/odoo-semantic-backup.service    /etc/systemd/system/
sudo cp docs/deploy/odoo-semantic-backup.timer      /etc/systemd/system/
sudo cp docs/deploy/osm-alert@.service              /etc/systemd/system/
sudo cp docs/deploy/osm-ttl-cleanup.service         /etc/systemd/system/
sudo cp docs/deploy/osm-ttl-cleanup.timer           /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Provision secrets (one-time, mode 600)
sudo install -o odoo-semantic -g odoo-semantic -m 700 -d /home/odoo-semantic/etc
sudo install -o odoo-semantic -g odoo-semantic -m 600 /dev/null /home/odoo-semantic/etc/webui.env
sudo tee /home/odoo-semantic/etc/webui.env > /dev/null <<EOF
FERNET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
WEBUI_SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
EOF
sudo chown odoo-semantic:odoo-semantic /home/odoo-semantic/etc/webui.env

# 4. Enable + start
sudo systemctl enable --now odoo-semantic-mcp.service
sudo systemctl enable --now odoo-semantic-webui.service
sudo systemctl enable --now odoo-semantic-astro.service
sudo systemctl enable --now odoo-semantic-backup.timer
sudo systemctl enable --now osm-ttl-cleanup.timer

# 5. Smoke-test
sudo systemctl status odoo-semantic-mcp.service \
                     odoo-semantic-webui.service \
                     odoo-semantic-astro.service
```

---

## 4. First-install procedure (non-canonical layout)

If your paths differ from the canonical layout, install drop-in
overrides **before** enabling the units:

```bash
# 1. Install shipped unit bodies (unchanged)
sudo cp docs/deploy/odoo-semantic-mcp.service /etc/systemd/system/
# ... etc

# 2. Create drop-in directories
sudo mkdir -p /etc/systemd/system/odoo-semantic-mcp.service.d
# ... per unit you need to customize

# 3. Copy + edit a matching template
sudo cp docs/deploy/overrides/odoo-semantic-mcp.service.d/local-paths.conf.example \
       /etc/systemd/system/odoo-semantic-mcp.service.d/local-paths.conf
sudo nano /etc/systemd/system/odoo-semantic-mcp.service.d/local-paths.conf

# 4. Reload + verify merged body
sudo systemctl daemon-reload
systemctl cat odoo-semantic-mcp.service
# Inspect output: shipped body + drop-in appended → effective directives

# 5. Enable + start (only after drift audit returns clean)
make check-systemd-overrides
sudo systemctl enable --now odoo-semantic-mcp.service
```

---

## 5. Routine upgrade procedure

Whenever a release ships changes under `docs/deploy/*.service`:

```bash
# 1. Drift audit BEFORE pulling — confirms current state is clean
make check-systemd-overrides
# If drift is reported, fix it first (move in-body edits to drop-in)

# 2. Pull the new release
git pull origin master

# 3. Diff the new shipped unit bodies vs installed
for u in odoo-semantic-mcp odoo-semantic-webui odoo-semantic-astro odoo-semantic-backup; do
    diff -u "/etc/systemd/system/${u}.service" "docs/deploy/${u}.service" || true
done
# Review each diff: are the changes safe to roll out? Any new required
# directives that my drop-in needs to also override?

# 4. Install the new shipped bodies
sudo install -m 644 docs/deploy/odoo-semantic-*.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Verify drop-ins still merge cleanly
for u in odoo-semantic-mcp odoo-semantic-webui odoo-semantic-astro odoo-semantic-backup; do
    systemctl cat "${u}.service"
done
# Confirm effective ExecStart=, EnvironmentFile=, ReadWritePaths= match
# what you expect.

# 6. Restart services one at a time, watching journal
sudo systemctl restart odoo-semantic-mcp.service
sudo journalctl -u odoo-semantic-mcp.service -f
# Look for: dev-mode fallback warnings (= env file path broken),
#           CHDIR failures (= WorkingDirectory unreachable),
#           Permission denied (= ProtectHome= too strict for new path).
```

---

## 6. Recovery procedure

If a deploy leaves the unit body in a broken state and you have a
working backup:

```bash
# 1. List installed unit bodies (and their override.conf files)
ls -la /etc/systemd/system/odoo-semantic-*.service{,.d/}

# 2. Restore from backup
sudo install -m 644 /path/to/backup/odoo-semantic-mcp.service \
                    /etc/systemd/system/

# 3. Apply any new upstream-only directives surgically (e.g. an
#    OnFailure= bug fix from a recent PR)
sudo sed -i 's|OnFailure=osm-alert@%n\.service|OnFailure=osm-alert@%n|' \
       /etc/systemd/system/odoo-semantic-mcp.service

# 4. Reload + restart
sudo systemctl daemon-reload
sudo systemctl restart odoo-semantic-mcp.service

# 5. After recovery, MIGRATE the customizations to a drop-in
#    override so the next upgrade does not re-trigger the outage.
#    Identify which directives differ from shipped and move them.
```

The 2026-05-19 outage was recovered this way (see issue #144 §"Recovery
on this host"), but the migration-to-drop-in step is the only durable
fix. If you find yourself doing step 3 more than once, you have not
yet moved the customizations into a drop-in.

---

## 7. Directives operators may legitimately override

Per `docs/deploy/overrides/README.md`:

- **OK:** `User=`, `Group=`, `WorkingDirectory=`, `ExecStart=` (with
  reset), `Environment=ODOO_SEMANTIC_CONF=`, `EnvironmentFile=` (path),
  `ReadWritePaths=` (additional paths).
- **OK with caveats:** `ProtectHome=read-only` ↔ `false` (but
  **never** `true` under `/home/...` layouts — see ADR-0027 §3 +
  issue #144).
- **Do not override:** `OnFailure=osm-alert@%n`, `Requires=docker.service`,
  `StartLimitBurst=`, `StartLimitIntervalSec=`, `NoNewPrivileges=true`,
  `ProtectSystem=strict`.

If you need to override one of the "do not override" set, file an issue
first — that probably indicates an upstream bug.

---

## 8. Related runbooks + ADRs

- [`docs/adr/0027-system-user-deployment.md`](../adr/0027-system-user-deployment.md) — canonical layout authority
- [`docs/deploy.md §3.5`](../deploy.md#35-systemd-services-mcp--fastapi--astro) — high-level deploy flow
- [`docs/deploy/m9-postmerge-ops.md §6`](m9-postmerge-ops.md) — TTL cleanup timer setup
- [`docs/deploy/overrides/README.md`](overrides/README.md) — drop-in semantics + ready examples
- [`scripts/check-systemd-overrides.sh`](../../scripts/check-systemd-overrides.sh) — drift audit script
