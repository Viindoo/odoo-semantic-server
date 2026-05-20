# Systemd drop-in overrides

Operators on **non-canonical deployment layouts** (anything other than
the ADR-0027 system-user layout at `/home/odoo-semantic/...`) should
use **systemd drop-in overrides** instead of editing the shipped unit
files in `docs/deploy/*.service` directly.

## Why drop-ins instead of editing the unit body

The shipped unit files evolve with each release — new directives
(e.g. `OnFailure=`, `ReadWritePaths=`, `TMPDIR=`), tightened security
hardening, or fixes to bugs like the
[issue #144 `ProtectHome=true`](https://github.com/Viindoo/odoo-semantic-mcp/issues/144)
regression. If you edit the unit body in place to fit your local
paths, the next routine upstream upgrade either:

- **(a) Wipes your edits silently** if you re-install with `cp` /
  `install -m 644`, causing the same outage as #144 (Astro CHDIR
  fail + `EnvironmentFile=` removed → silent dev-mode fallback for
  mcp + webui).
- **(b) Stalls the upgrade** while you re-diff and reconcile by hand,
  during which you may miss a security-relevant directive change.

Drop-in overrides at `/etc/systemd/system/<unit>.service.d/override.conf`
solve both problems: upstream owns the body, operators own the override.
`systemctl daemon-reload` automatically merges them at unit-load time.

## Workflow

For each unit you need to customize:

```bash
sudo mkdir -p /etc/systemd/system/odoo-semantic-mcp.service.d
sudo cp docs/deploy/overrides/odoo-semantic-mcp.service.d/local-paths.conf.example \
       /etc/systemd/system/odoo-semantic-mcp.service.d/local-paths.conf
sudo nano /etc/systemd/system/odoo-semantic-mcp.service.d/local-paths.conf  # edit paths
sudo systemctl daemon-reload
sudo systemctl restart odoo-semantic-mcp.service
```

Verify the merged unit body matches expectations:

```bash
systemctl cat odoo-semantic-mcp.service
# Output: shipped unit body + override.conf appended below it
```

## What you can override (and what you should not)

`man systemd.unit` covers the full merge semantics. The practical rules:

| Directive type | Override behavior | Example |
|---|---|---|
| Single-valued (most `[Service]` keys) | **Overrides** — last definition wins | `WorkingDirectory=`, `User=`, `ProtectHome=` |
| `Environment=` (also key-by-key) | **Appends + overrides keys** | Add `Environment=DEBUG=1` without losing existing `HOST=`/`PORT=` |
| List-valued (`EnvironmentFile=`, `ReadWritePaths=`, `OnFailure=`, `After=`) | **Appends** by default | Add more `EnvironmentFile=` lines |
| List-valued **reset**: prefix one empty assignment | **Resets then appends** | `EnvironmentFile=` (empty) followed by your own |
| `ExecStart=` | Special: **must reset with empty `ExecStart=`** before redefining | See `odoo-semantic-mcp.service.d/alt-python.conf.example` |

### Directives operators **may** override

- `User=`, `Group=` (if running as a different account)
- `WorkingDirectory=` (alternate app dir)
- `ExecStart=` (alternate venv path — needs reset pattern)
- `Environment=ODOO_SEMANTIC_CONF=` (alternate config dir)
- `EnvironmentFile=` (alternate `.env` / `webui.env` paths — append)
- `ReadWritePaths=` (additional writable carve-outs — append)
- `ProtectHome=` (only `read-only` or `false`, **never** `true` —
  see ADR-0027 §3 + issue #144)

### Directives operators **should not** override

- `OnFailure=osm-alert@%n` — fire-and-forget alert chain, breaking
  it silently hides production failures
- `Requires=docker.service` — DB tier dependency contract
- `StartLimitBurst=` / `StartLimitIntervalSec=` — restart-loop guard
- `NoNewPrivileges=true` / `ProtectSystem=strict` — baseline hardening

If you need to override one of these, file an issue first — it
probably indicates an upstream bug.

## Files in this directory

| File | Use case |
|---|---|
| `odoo-semantic-mcp.service.d/local-paths.conf.example` | Alternate app dir / venv / config dir |
| `odoo-semantic-webui.service.d/local-paths.conf.example` | Alternate app dir / env files |
| `odoo-semantic-astro.service.d/local-paths.conf.example` | Alternate app dir / Node binary |
| `odoo-semantic-backup.service.d/local-paths.conf.example` | Alternate `BACKUP_DIR`, `TMPDIR`, app dir |
| `odoo-semantic-mcp.service.d/opt-layout.conf.example` | Legacy `/opt/odoo-semantic-mcp` layout |

Copy the closest match, edit paths, install at
`/etc/systemd/system/<unit>.service.d/<your-name>.conf`. You can
keep multiple drop-ins per unit if it helps you organize (e.g.
`50-local-paths.conf` + `60-debug-env.conf`).

## `install.sh --with-overrides` semantics

`bash install.sh --systemd --with-overrides` lays down scaffolds from
`docs/deploy/overrides/*.service.d/*.example` into
`/etc/systemd/system/<unit>.service.d/`, regardless of whether the
corresponding `<unit>.service` was newly installed, in-sync, or
skipped-divergent in the same run. This is **intentional** — when a
unit is reported divergent by the idempotency check, the operator
typically wants the scaffold so they can move in-body customizations
into a drop-in and re-run `install.sh` cleanly. Scaffolds never
overwrite an existing operator-edited drop-in (per-file `[[ -f ]]`
guard), so re-runs are safe.

Workflow after a divergent skip:
1. Edit the scaffold under `/etc/systemd/system/<unit>.service.d/`
   with your operator-specific values (the body shows placeholder
   paths the example was written against).
2. `sudo systemctl daemon-reload`
3. `systemctl cat <unit>.service` — verify the merged body matches
   what you ran with before the divergence was detected.
4. Re-run `bash install.sh --systemd` — divergence should clear
   (shipped body matches what operator-customizations now live in
   the drop-in).

## Drift detection

`make check-systemd-overrides` reports any installed unit body that
diverges from the shipped template (signalling that someone edited
the body in place instead of using a drop-in). Run before every
deploy. The script is also installed at
`scripts/check-systemd-overrides.sh` for direct invocation.
