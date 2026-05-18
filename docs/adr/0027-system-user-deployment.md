# ADR-0027 — System-User Deployment Layout

**Status:** Accepted

---

## Context

OSM can be deployed under either a personal login user or a dedicated system account. Running
the service as a personal login user introduces operational friction:

1. `ProtectHome=true` in systemd units bind-mounts `/home/*` to an empty tmpfs, hiding the venv
   and app binaries from the service. The mistake is easy to make and the failure mode is opaque.
2. Backup CLI staging (a full `pg_dump` output plus the resulting `.tar.gz`) competes with other
   tmpfs consumers when `/tmp` is RAM-backed and the database is multi-GB.
3. Service file ownership entangles with the operator's personal shell activity — `chown` on
   indexed clones, audit log writes, and SSH key encryption all share an inode space with files
   the operator may touch interactively.
4. Backup/restore tooling and any cross-server migration script must hard-code paths that depend
   on `$HOME` of a specific human, breaking when operators rotate.

This ADR specifies the recommended layout: a dedicated system user with stable, predictable
absolute paths, and the systemd / venv / backup gotchas that the layout interacts with.

---

## Decision

### §1 — System user `odoo-semantic`

Production runs as a dedicated system user `odoo-semantic` created with
`--system --shell /bin/bash --create-home`. The user must be in the `docker` group so the backup
CLI can exec into containers.

```bash
sudo useradd --system --shell /bin/bash --create-home \
             --comment "Odoo Semantic MCP service account" odoo-semantic
sudo usermod -aG docker odoo-semantic
```

All service processes (`odoo-semantic-mcp.service`, `odoo-semantic-webui.service`,
`odoo-semantic-astro.service`, `odoo-semantic-backup.service`) carry `User=odoo-semantic`.

### §2 — Directory layout

| Resource | Path |
|---|---|
| App directory | `/home/odoo-semantic/odoo-semantic-mcp/` |
| Python venv | `/home/odoo-semantic/.venv/odoo-semantic-mcp/` |
| Config directory | `/home/odoo-semantic/etc/` (mode 700) |
| Main config | `/home/odoo-semantic/etc/odoo-semantic.conf` (mode 600) |
| Web UI env | `/home/odoo-semantic/etc/webui.env` (mode 600) |
| Indexed repo clones | `/home/odoo-semantic/repos/` |
| Backup output | `/var/backups/odoo-semantic/` |
| Backup tmpdir | `/var/tmp` (via `Environment=TMPDIR=/var/tmp` in backup unit) |

Config directory at `/home/odoo-semantic/etc/` (mode 700 dir, mode 600 files) is chosen over
`/etc/odoo-semantic/` because `ProtectHome=read-only` (§3 below) keeps these config files
accessible to the service without requiring `ReadWritePaths=/etc/odoo-semantic` override.

**Note for operators preferring `/etc/` placement:** put config in `/etc/odoo-semantic/` and add
`ReadWritePaths=/etc/odoo-semantic` to the unit file. Both approaches are valid; the home-dir
convention is simpler when `ProtectHome=read-only` is in use.

### §3 — `ProtectHome=read-only` (not `ProtectHome=true`)

systemd units use `ProtectHome=read-only`, **not** `ProtectHome=true`.

The distinction matters when service binaries and the venv live under `/home/<service-user>/`:

| Setting | Effect |
|---|---|
| `ProtectHome=true` | Makes all `/home/*` directories **empty** (bind-mounts to empty tmpfs). Service cannot see its own venv. |
| `ProtectHome=read-only` | Mounts `/home/*` read-only. Service can read its venv and static configs. Writes (e.g. log files, DB sockets) are blocked for `/home/*` paths but allowed via `ReadWritePaths=` overrides. |

Explicit `ReadWritePaths=` lines in each unit file cover the writable paths the service actually
needs (e.g. clone directory, backup output directory).

### §4 — `TMPDIR=/var/tmp` for the backup unit

The backup systemd unit must set `Environment=TMPDIR=/var/tmp`.

The backup CLI (`src/cli.py backup`) stages two large files before tar-packing: a full `pg_dump`
output (can exceed several GB for a mature production DB) and the resulting archive. On hosts
where `/tmp` is a tmpfs sized to physical RAM, both the intermediate file and the final archive
compete for that same memory-backed filesystem. The resulting RAM spike can cause the OS to kill
the backup process (OOM) or exhaust available tmpfs space.

`/var/tmp` is a disk-backed temporary directory with persistent semantics (files survive reboots).
It is appropriate for large, long-lived temporaries. Switch:

```ini
[Service]
Environment="TMPDIR=/var/tmp"
```

Add this to `odoo-semantic-backup.service` (in addition to the existing `ODOO_SEMANTIC_CONF` env).
The shipped backup unit file (`docs/deploy/odoo-semantic-backup.service`) should carry this line.

### §5 — `uv`-managed venv: no `bin/pip`, use `uv pip install`

When the venv is created by `uv` (via `make install`), there is **no** `<venv>/bin/pip` binary.
Attempting `<venv>/bin/pip install ...` fails with "No such file or directory".

Use `uv pip install --python <venv>/bin/python -e ".[dev]"` for any post-install dependency
additions or in-place re-installs after a `git pull`:

```bash
uv pip install \
    --python /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
    -e ".[dev]"
```

The `.[all]` extra does not exist (only `.[dev]` and `.[integration]` are declared in
`pyproject.toml`). Using `.[all]` raises `Extra 'all' is not defined`.

### §6 — Docker Compose project name = directory basename

Docker Compose derives the project name from the basename of the working directory by default.
A deployment can relocate the app directory without running `docker compose down/up`, provided
the new directory basename is identical and the named Docker volumes remain intact.

If the basename changes (e.g. `odoo-semantic-mcp` → `osm`), volumes from the old project are
orphaned under the old prefix. Always verify project name after any directory move:

```bash
docker compose ls        # shows project name + status
docker volume ls         # check volume prefix matches project name
```

### §7 — `repos.local_path` UPDATE after moving indexed clones

After rsync'ing indexed repo clones to a new path, the `repos` table in PostgreSQL must be
updated to reflect the new `local_path` for every repo row. Failure to do this causes the
indexer to look for repos at the old path and fail silently (the profiled repo is skipped, no
re-index error is raised).

```sql
-- Run as the DB owner, replacing old_prefix with the previous clone root path:
UPDATE repos
SET local_path = replace(local_path, '<old_clone_root>/', '<new_clone_root>/')
WHERE local_path LIKE '<old_clone_root>/%';
```

Verify the row count matches the total number of registered repos before restarting the indexer.

---

## Rationale

- **Separation from personal user:** service processes do not share file permissions, environment
  variables, or shell history with any human operator's login session.
- **`ProtectHome=read-only` hardening compatibility:** venv and configs remain readable while the
  home directory is otherwise locked down.
- **Predictable paths for backup/restore tooling:** absolute paths in
  `docs/deploy/odoo-semantic-mcp.service` and the backup unit are stable across operator changes
  (no dependency on `$HOME` of a specific person).
- **`/var/tmp` for backup staging:** eliminates OOM kills on constrained RAM/tmpfs hosts without
  requiring the operator to size up physical RAM.

---

## Migration path (for existing personal-user deployments)

Deployments still running as a personal login user can continue to function. Migration is
recommended but not forced. To move an existing personal-user deployment to the system-user
layout:

1. Create the `odoo-semantic` system user (§1).
2. `git clone` or `rsync` the app directory to `/home/odoo-semantic/odoo-semantic-mcp/`.
3. Run `sudo -u odoo-semantic -H bash -c 'cd /home/odoo-semantic/odoo-semantic-mcp && make install'`
   to create the venv under the service account.
4. Copy config files to `/home/odoo-semantic/etc/` with `chmod 700` dir + `chmod 600` files.
5. `rsync` indexed clone directories to `/home/odoo-semantic/repos/`.
6. Update `repos.local_path` in the database (§7).
7. Update and reload systemd unit files (`User=odoo-semantic`, updated paths).
8. Update Docker Compose project name if the app directory basename changed (§6).

Full step-by-step deploy runbook: [`docs/deploy.md §3`](../deploy.md#3-app-tier).

---

## Related ADRs

- **ADR-0006** — Environment harness (M6 Wave 1). Docker volume and container naming conventions.
- **ADR-0008** — SSH auto-clone. Clone directory conventions referenced here (§7).
- **ADR-0018** — Backup bundle contract. Backup output path and file naming policy.
- **ADR-0019** — Restore upload security. `/var/tmp` staging aligns with §4 here.
