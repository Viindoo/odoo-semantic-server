#!/usr/bin/env bash
# server-setup.sh — idempotent host provisioning for the osm-mcp server.
#
# Turns this machine into the osm server: installs dependencies, sets up
# PostgreSQL, creates the restricted `osm` Linux user, builds a dedicated
# Python venv for the server, installs the /usr/local/bin/osm-stdio shim,
# and hardens sshd.
#
# Implements deployment-dev-host.md §2 (manual equivalent).
# Sections §4 (Windows-host/router/DDNS) are OUT OF SCOPE — see the runbook.
#
# PostgreSQL auth strategy
# ------------------------
# Peer auth: inserts a `local osm_17,osm_18,osm_19 osm peer` line BEFORE
# the first existing `local` rule in pg_hba.conf so it is not shadowed by
# a broader rule. If you prefer password auth, set --pg-password on this
# script; it will then write ~osm/.pgpass (mode 600, owned by osm) instead
# of the peer hba entry. Either way, no password is stored in the installed
# scripts.
#
# Server Python venv (/home/osm/venv)
# ------------------------------------
# sshd ForceCommand runs as the restricted Linux user `osm` who has no `uv`
# and cannot reach the dev user's ~/.local/bin/uv.  server-setup.sh builds a
# dedicated venv at /home/osm/venv using the dev user's uv, installs the
# package in editable mode so dev edits are picked up immediately, then
# chown-s the venv to osm.  osm-stdio execs venv/bin/python directly with no
# uv involved at request time.  The venv path can be overridden with
# --osm-venv (default: /home/<osm-user>/venv).
#
# /usr/local/bin/osm-stdio — shim vs copy
# -----------------------------------------
# We install a tiny shim that exec-s the repo's scripts/osm-stdio with the
# dev user's repo path and OSM_VENV baked in.  This keeps one authoritative
# copy of the logic in scripts/osm-stdio; the shim just anchors the paths.
#
# DO NOT EXECUTE THIS SCRIPT automatically. Run it manually as your dev
# user (step 3 in deployment-dev-host.md). It calls `sudo apt-get install`,
# creates Linux users, edits sshd config, and reloads sshd.

set -euo pipefail

# ---------- helpers ----------
info()  { printf '\033[1;34m[server-setup]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[server-setup]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[server-setup WARN]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[server-setup ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: server-setup.sh [OPTIONS]

Idempotent host provisioning: uv, PostgreSQL, databases, restricted
SSH user, server venv, sshd hardening. Implements deployment-dev-host.md §2.

Options:
  --repo-path PATH   Path to the odoo-semantic-mcp repo checkout.
                     Default: directory containing this script.
  --no-apt           Skip apt-get package installation (for re-runs).
  --port N           Add a second SSH Port directive (e.g. 2222) for the
                     non-standard port to forward. Default: only 22.
                     Must be a valid port number (1–65535).
  --osm-user USER    Linux username for the restricted MCP user.
                     Default: osm.
  --osm-venv PATH    Path for the server Python venv.
                     Default: /home/<osm-user>/venv.
  --pg-password PW   Use password auth for the osm PG role instead of peer
                     auth. Writes ~osm/.pgpass (600, owned by osm).
  -h, --help         Show this help.
EOF
}

# ---------- defaults ----------
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
SKIP_APT=false
EXTRA_SSH_PORT=""
PG_PASSWORD=""
OSM_USER="osm"
OSM_VENV=""  # resolved after OSM_USER is known

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-path)   REPO_PATH="$2";       shift 2 ;;
    --no-apt)      SKIP_APT=true;        shift ;;
    --port)        EXTRA_SSH_PORT="$2";  shift 2 ;;
    --pg-password) PG_PASSWORD="$2";     shift 2 ;;
    --osm-user)    OSM_USER="$2";        shift 2 ;;
    --osm-venv)    OSM_VENV="$2";        shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

# ---------- post-parse validation ----------
[[ -d "$REPO_PATH" ]] || die "Repo path does not exist: $REPO_PATH"
[[ -f "$REPO_PATH/pyproject.toml" ]] || die "Not an osm-mcp repo: $REPO_PATH"

if [[ -n "$EXTRA_SSH_PORT" ]]; then
  [[ "$EXTRA_SSH_PORT" =~ ^[0-9]+$ ]] || die "--port must be a number (got: $EXTRA_SSH_PORT)"
  if [[ "$EXTRA_SSH_PORT" -lt 1 || "$EXTRA_SSH_PORT" -gt 65535 ]]; then
    die "--port must be between 1 and 65535 (got: $EXTRA_SSH_PORT)"
  fi
fi

# Resolve venv path default now that OSM_USER is settled
if [[ -z "$OSM_VENV" ]]; then
  OSM_VENV="/home/${OSM_USER}/venv"
fi

OSM_HOME="/home/${OSM_USER}"

info "Repo path:   $REPO_PATH"
info "osm user:    $OSM_USER"
info "osm venv:    $OSM_VENV"
info "Skip apt:    $SKIP_APT"
[[ -n "$EXTRA_SSH_PORT" ]] && info "Extra SSH port: $EXTRA_SSH_PORT"

# ---------- step 1: uv ----------
info "=== Step 1: uv ==="
if command -v uv >/dev/null 2>&1; then
  ok "uv already installed: $(uv --version)"
else
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # Source the env script if it exists so uv is on PATH for subsequent steps
  if [[ -f "$HOME/.cargo/env" ]]; then
    # shellcheck source=/dev/null
    source "$HOME/.cargo/env"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installation failed; add ~/.local/bin to PATH and retry"
  ok "uv installed: $(uv --version)"
fi
UV_BIN="$(command -v uv)"

# ---------- step 2: apt packages ----------
if [[ "$SKIP_APT" == false ]]; then
  info "=== Step 2: apt packages ==="
  sudo apt-get update -qq
  # Try postgresql-16; if not available fall back to whatever is installable
  if apt-cache show postgresql-16 >/dev/null 2>&1; then
    PG_PKG="postgresql-16 postgresql-16-pgvector"
  else
    warn "postgresql-16 not in apt cache; installing postgresql (latest available)"
    PG_PKG="postgresql postgresql-contrib"
    warn "pgvector may need manual install if postgresql-<ver>-pgvector is unavailable"
  fi
  # SC2086: word-splitting on PG_PKG is intentional — it's a space-separated list of package names
  # shellcheck disable=SC2086
  sudo apt-get install -y $PG_PKG postgresql-contrib libxml2-dev libxslt1-dev fail2ban
  ok "Packages installed"
else
  info "=== Step 2: apt (skipped via --no-apt) ==="
fi

# ---------- helper: locate pg_hba.conf ----------
_pg_hba_conf() {
  sudo -u postgres psql -tAc "SHOW hba_file;" 2>/dev/null | tr -d ' '
}

# ---------- step 3: PostgreSQL role + DBs + pgvector ----------
info "=== Step 3: PostgreSQL role, databases, pgvector ==="

# Ensure postgres service is running
if ! sudo systemctl is-active --quiet postgresql; then
  sudo systemctl start postgresql
fi

# Create role `osm` (idempotent)
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${OSM_USER}';" | grep -q 1; then
  ok "PG role '${OSM_USER}' already exists"
else
  sudo -u postgres createuser --no-superuser --no-createrole --createdb "$OSM_USER"
  ok "PG role '${OSM_USER}' created"
fi

# Set password if requested — use psql stdin to keep the secret out of
# the process list and to avoid shell quoting issues with special chars.
if [[ -n "$PG_PASSWORD" ]]; then
  # Escape single quotes by doubling them for the SQL literal
  PG_PASSWORD_ESCAPED="${PG_PASSWORD//"'"/"''"}"
  printf "ALTER ROLE %s PASSWORD '%s';\n" "$OSM_USER" "$PG_PASSWORD_ESCAPED" \
    | sudo -u postgres psql -f - >/dev/null
  ok "Password set on role '${OSM_USER}'"
fi

# Create DBs + extension (idempotent)
for v in 17 18 19; do
  DB="osm_${v}"
  if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}';" | grep -q 1; then
    ok "DB '${DB}' already exists"
  else
    sudo -u postgres createdb -O "$OSM_USER" "$DB"
    ok "DB '${DB}' created"
  fi
  sudo -u postgres psql -d "$DB" -c 'CREATE EXTENSION IF NOT EXISTS vector;' -q
  ok "Extension vector present in ${DB}"
done

# ---------- step 4: pg_hba.conf (peer or password) ----------
info "=== Step 4: pg_hba.conf ==="

# Preflight: sshd_config.d Include check happens in step 8; similar pattern here.
HBA_FILE="$(_pg_hba_conf)"
if [[ -z "$HBA_FILE" || ! -f "$HBA_FILE" ]]; then
  warn "Could not locate pg_hba.conf automatically."
  warn "Please add the following line manually BEFORE the first 'local' rule"
  warn "and reload PostgreSQL:"
  warn "  local   osm_17,osm_18,osm_19   ${OSM_USER}   peer"
else
  if [[ -n "$PG_PASSWORD" ]]; then
    AUTH_LINE="local   osm_17,osm_18,osm_19   ${OSM_USER}   md5"
    AUTH_DESC="md5 (password)"
  else
    AUTH_LINE="local   osm_17,osm_18,osm_19   ${OSM_USER}   peer"
    AUTH_DESC="peer"
  fi

  if sudo grep -qF "osm_17,osm_18,osm_19" "$HBA_FILE"; then
    ok "pg_hba.conf already has ${OSM_USER} auth rule"
  else
    # Insert BEFORE the first `local`-type rule so we are not shadowed by
    # a broader `local all all peer/md5` line (PG uses first-match).
    if sudo grep -qE '^[[:space:]]*local[[:space:]]' "$HBA_FILE"; then
      # Insert our rule BEFORE the first `local`-type line using sed `i`
      # (insert). `0,/pattern/` limits to only the first match so subsequent
      # `local` lines are untouched. AUTH_LINE contains only [a-z0-9_, /]
      # so it is safe to interpolate into the sed script.
      sudo sed -i "0,/^[[:space:]]*local[[:space:]]/{/^[[:space:]]*local[[:space:]]/i\\
${AUTH_LINE}
}" "$HBA_FILE"
      ok "Inserted ${AUTH_DESC} auth rule before first 'local' entry in $HBA_FILE"
    else
      # No existing local rules — safe to append
      printf '%s\n' "$AUTH_LINE" | sudo tee -a "$HBA_FILE" >/dev/null
      ok "Appended ${AUTH_DESC} auth rule to $HBA_FILE (no prior local rules)"
    fi
    sudo systemctl reload postgresql
    ok "PostgreSQL reloaded"
  fi

  # Connectivity check: confirm osm can connect via the new rule
  if sudo -u "$OSM_USER" psql -d osm_17 -c 'SELECT 1' >/dev/null 2>&1; then
    ok "Connectivity check passed: ${OSM_USER} can connect to osm_17"
  else
    warn "${OSM_USER} cannot connect to osm_17 — check pg_hba.conf ordering"
    warn "Ensure the osm rule appears BEFORE any broader 'local all all' rule."
  fi
fi

# Compute ~/.pgpass content (used after osm user is created in step 6)
if [[ -n "$PG_PASSWORD" ]]; then
  # Build pgpass lines without printf %b to avoid backslash expansion in passwords
  PGPASS_LINES=()
  for v in 17 18 19; do
    PGPASS_LINES+=("localhost:5432:osm_${v}:${OSM_USER}:${PG_PASSWORD}")
    PGPASS_LINES+=("/var/run/postgresql:5432:osm_${v}:${OSM_USER}:${PG_PASSWORD}")
  done
fi

# ---------- step 5: Linux user osm + venv setup (before migrations) ----------
info "=== Step 5: Linux user '${OSM_USER}' ==="
if id "$OSM_USER" >/dev/null 2>&1; then
  ok "User '${OSM_USER}' already exists"
else
  sudo useradd -m -s /bin/bash "$OSM_USER"
  ok "User '${OSM_USER}' created"
fi

# .ssh directory
if [[ ! -d "${OSM_HOME}/.ssh" ]]; then
  sudo install -d -m 700 -o "$OSM_USER" -g "$OSM_USER" "${OSM_HOME}/.ssh"
fi
if [[ ! -f "${OSM_HOME}/.ssh/authorized_keys" ]]; then
  sudo touch "${OSM_HOME}/.ssh/authorized_keys"
  sudo chown "${OSM_USER}:${OSM_USER}" "${OSM_HOME}/.ssh/authorized_keys"
  sudo chmod 600 "${OSM_HOME}/.ssh/authorized_keys"
fi
ok "~${OSM_USER}/.ssh/authorized_keys ready"

# Write ~/.pgpass if password auth
if [[ -n "$PG_PASSWORD" ]]; then
  {
    for line in "${PGPASS_LINES[@]}"; do
      printf '%s\n' "$line"
    done
  } | sudo tee "${OSM_HOME}/.pgpass" >/dev/null
  sudo chown "${OSM_USER}:${OSM_USER}" "${OSM_HOME}/.pgpass"
  sudo chmod 600 "${OSM_HOME}/.pgpass"
  ok "~${OSM_USER}/.pgpass written (mode 600)"
fi

# ---------- step 6: server Python venv ----------
info "=== Step 6: Server Python venv at ${OSM_VENV} ==="
# Build the venv as the dev user (who has uv), then chown to osm.
# The package is installed in editable mode so dev edits are picked up
# without rebuilding the venv.
#
# Permission dance (Gap 1): useradd -m creates /home/<osm-user> as 0750
# osm:osm.  The dev user cannot write there (not owner, not in group with
# write, not world-writable).  We temporarily grant the dev user ownership
# of /home/<osm-user> (owner:group stays dev:osm, mode stays 0750 — we only
# touch the owner, not the mode, so no extra world-permissions are added).
# A trap guarantees the restore even if uv fails mid-build: without it,
# set -euo pipefail would exit early leaving /home/<osm-user> owned by the
# dev user permanently, and the stat guard on re-run would skip the restore
# entirely (seeing the dev user as already-owner) → osm can't read its own
# ~/.ssh/authorized_keys / .pgpass → ForceCommand silently broken.
DEV_USER="$(id -un)"

# Trap: always restore $OSM_HOME to osm:osm on EXIT.  Both chown ops below are
# idempotent so the happy path just gets two chowns instead of one.
trap 'sudo chown -R "${OSM_USER}:${OSM_USER}" "$OSM_HOME" 2>/dev/null || true' EXIT

sudo chown "${DEV_USER}:${OSM_USER}" "$OSM_HOME"

if [[ ! -x "${OSM_VENV}/bin/python" ]]; then
  info "  Creating venv..."
  "$UV_BIN" venv "$OSM_VENV"
  ok "  Venv created: $OSM_VENV"
else
  ok "  Venv already exists: $OSM_VENV"
fi

info "  Installing package (editable)..."
"$UV_BIN" pip install -e "$REPO_PATH" --python "${OSM_VENV}/bin/python"
ok "  Package installed (editable)"

# Restore $OSM_HOME to osm:osm now (happy path).  The trap fires on EXIT too,
# so this is defence-in-depth; the trap is cleared below after the venv chown.
sudo chown "${OSM_USER}:${OSM_USER}" "$OSM_HOME"

# Grant the osm user read+exec on the repo (it's OSS — not secret)
# so it can import source modules via the editable install.
chmod -R a+rX "$REPO_PATH"
ok "  chmod a+rX $REPO_PATH"

# Add a world-traversable (o+x) bit on every parent directory of REPO_PATH
# up to / so the osm user can cd into the repo.  We intentionally add only
# the execute (traversal) bit — NOT o+r — so home dirs remain non-listable
# (non-listable = world cannot run ls there, but can cd through).
# This is idempotent: chmod o+x is a no-op when the bit is already set.
# We announce any directory where the bit was NOT already present so the
# operator is aware — especially if /home/${DEV_USER} is hardened to 750/700.
_dir="$(dirname "$(realpath "$REPO_PATH")")"
while [[ "$_dir" != "/" ]]; do
  # Check current o+x state before changing (stat: extract "others execute" bit).
  if [[ "$(stat -c '%a' "$_dir")" =~ [1357]$ ]]; then
    : # o+x already set — no-op, no announcement needed
  else
    info "  Adding o+x (traverse-only, NOT o+r) to: $_dir"
    chmod o+x "$_dir" 2>/dev/null || sudo chmod o+x "$_dir"
  fi
  _dir="$(dirname "$_dir")"
done
ok "  World-traversable (o+x) bits verified on parent dirs of $REPO_PATH"

# Transfer venv ownership to osm so osm can write .pth / __pycache__
sudo chown -R "${OSM_USER}:${OSM_USER}" "$OSM_VENV"
ok "  chown ${OSM_USER}:${OSM_USER} $OSM_VENV"

# $OSM_HOME is now osm:osm and the venv is osm:osm — cancel the EXIT trap.
trap - EXIT

# Warn about Odoo source directories (Gap 2).
# The indexer (scripts/index.py) is run as '${OSM_USER}', so all Odoo source
# directories it reads must also be world-traversable (o+x on every parent)
# AND world-readable+executable (a+rX on the addons trees themselves).
# The script cannot know those paths in advance, so we emit a reminder.
warn "Odoo source dirs: ensure each Odoo addons tree is world-rX AND every"
warn "parent directory up to / has the traverse bit (o+x — NOT o+r).  Example:"
warn "  sudo chmod o+x /home/${DEV_USER}"
warn "  chmod -R a+rX ~/git/17.0/odoo/odoo/addons ~/git/17.0/odoo/addons"
warn "If those dirs are under /home/${DEV_USER}/ the o+x on /home/${DEV_USER} above covers traversal."
warn "Alternatively, run 'GRANT ${OSM_USER} TO ${DEV_USER}' in PostgreSQL and index as ${DEV_USER}."

# ---------- step 7: run migrations as osm ----------
info "=== Step 7: Run migrations (as ${OSM_USER}) ==="
for v in 17 18 19; do
  info "  Migrating osm_${v}..."
  sudo -u "$OSM_USER" bash -c \
    "cd '${REPO_PATH}' && DATABASE_URL='postgresql:///osm_${v}' '${OSM_VENV}/bin/python' scripts/migrate.py --schema public"
  ok "  osm_${v} migrated"
done

# ---------- step 8: /usr/local/bin/osm-stdio shim ----------
info "=== Step 8: Install /usr/local/bin/osm-stdio shim ==="
# The shim is a thin wrapper that exec-s the repo's scripts/osm-stdio.
# OSM_VENV is baked in so osm-stdio can locate the server Python directly
# without uv.
sudo tee /usr/local/bin/osm-stdio >/dev/null <<SHIM
#!/usr/bin/env bash
# Auto-generated by server-setup.sh — do not edit by hand.
# Shim for the restricted ${OSM_USER} user; exec-s the repo's scripts/osm-stdio.
export OSM_VENV="${OSM_VENV}"
exec "${REPO_PATH}/scripts/osm-stdio" "\$@"
SHIM
sudo chmod 755 /usr/local/bin/osm-stdio
ok "/usr/local/bin/osm-stdio installed (shim → $REPO_PATH/scripts/osm-stdio, venv=${OSM_VENV})"

# ---------- step 9: sshd drop-in ----------
info "=== Step 9: sshd drop-in ==="
SSHD_DROP="/etc/ssh/sshd_config.d/osm.conf"

# Preflight: verify main sshd_config includes the drop-in directory.
# Ubuntu 22.04+ and Debian 12+ add this by default; older systems may not.
grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config \
  || die "main sshd_config has no 'Include /etc/ssh/sshd_config.d/*.conf' — add it manually first (older Ubuntu/Debian)"

if [[ -n "$EXTRA_SSH_PORT" ]]; then
  PORT_LINES="Port 22
Port ${EXTRA_SSH_PORT}"
else
  PORT_LINES="# Add 'Port <N>' here to listen on a non-standard port for NAT/forwarding.
# See deployment-dev-host.md §4."
fi

sudo tee "$SSHD_DROP" >/dev/null <<SSHD
# osm-mcp sshd drop-in — generated by server-setup.sh
# Match User ${OSM_USER}: key-only, forced command, no TTY/forwarding.
# Do NOT add 'Port' directives here if you rely on /etc/ssh/sshd_config
# for the main listening port — adding Port here resets the defaults.
${PORT_LINES}

Match User ${OSM_USER}
    PasswordAuthentication no
    PubkeyAuthentication yes
    ForceCommand /usr/local/bin/osm-stdio
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
    AllowAgentForwarding no
    PermitTunnel no
    PermitUserRC no
    GatewayPorts no
    PermitOpen none
SSHD
ok "Wrote $SSHD_DROP"

# Validate config before restarting
info "Validating sshd config..."
sudo sshd -t || die "sshd -t failed — check $SSHD_DROP before restarting"
ok "sshd config valid"

# Restart sshd — handle both classic and socket-activated (Ubuntu 22.10+/24.04).
#
# On socket-activated systems (Ubuntu 22.10+), sshd is managed by ssh.socket.
# Port directives in sshd_config.d are honoured by the daemon config, but the
# listening socket itself is created by ssh.socket; adding a new Port only takes
# effect when ssh.socket is restarted (not just the ssh.service).  Restarting
# only ssh.service restarts the daemon and picks up sshd_config changes (auth
# rules, ForceCommand, etc.) but does NOT bind any new Port.
#
# Detection: if ssh.socket is an active unit, use it.  We always restart the
# daemon service too so config changes (ForceCommand, Match block, etc.) are
# applied immediately even when no new port was added.
_SOCKET_ACTIVE=false
if sudo systemctl is-active --quiet ssh.socket 2>/dev/null; then
  _SOCKET_ACTIVE=true
fi

if [[ "$_SOCKET_ACTIVE" == true ]]; then
  # Socket-activated path.  Restart ssh.socket first (rebinds all Port
  # directives including the new one), then the daemon service.
  if sudo systemctl restart ssh.socket 2>/dev/null; then
    ok "ssh.socket restarted (socket-activated sshd — new Port binds here)"
  else
    warn "Could not restart ssh.socket; run: sudo systemctl restart ssh.socket"
  fi
  # Daemon restart failure is fatal: socket bound but daemon dead = SSH endpoint
  # silently down.  The operator must fix this before continuing.
  if sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd 2>/dev/null; then
    ok "ssh daemon restarted"
  else
    die "ssh daemon restart failed — SSH endpoint may be down. Fix with: sudo systemctl restart ssh  Then re-run this script."
  fi
else
  # Classic path: no ssh.socket, just restart the service.
  if sudo systemctl restart ssh 2>/dev/null; then
    ok "ssh service restarted"
  elif sudo systemctl restart sshd 2>/dev/null; then
    ok "sshd service restarted"
  else
    die "ssh/sshd restart failed — SSH endpoint may be down. Fix with: sudo systemctl restart ssh  Then re-run this script."
  fi
fi

[[ -n "$EXTRA_SSH_PORT" ]] && info "Verify with: ss -tln | grep ':${EXTRA_SSH_PORT}'"

# ---------- step 10: fail2ban ----------
info "=== Step 10: fail2ban ==="
sudo systemctl enable --now fail2ban
ok "fail2ban enabled"

# ---------- summary ----------
cat <<SUMMARY

$(printf '\033[1;32m=== server-setup.sh complete ===\033[0m')

Done on this machine:
  - uv installed ($UV_BIN)
  - Packages: postgresql, pgvector, fail2ban, libxml2-dev, libxslt1-dev
  - PostgreSQL: role '${OSM_USER}', DBs osm_17/osm_18/osm_19, vector extension
  - pg_hba.conf: $(if [[ -n "$PG_PASSWORD" ]]; then echo "md5 password auth (see ~${OSM_USER}/.pgpass)"; else echo "peer auth for local socket (no password)"; fi)
  - Server venv: ${OSM_VENV} (editable install of $REPO_PATH)
  - Migrations applied to osm_17, osm_18, osm_19 (schema: public, as ${OSM_USER})
  - Linux user '${OSM_USER}' with ~${OSM_USER}/.ssh/authorized_keys
  - /usr/local/bin/osm-stdio shim → $REPO_PATH/scripts/osm-stdio
  - /etc/ssh/sshd_config.d/osm.conf (ForceCommand, no TTY/forwarding)
  - fail2ban enabled

Still required from YOU (the founder — see deployment-dev-host.md §4):
  - Choose a DDNS name (DuckDNS / Cloudflare) and a non-standard SSH port.
  - WSL2 networking: set networkingMode=mirrored in C:\Users\<you>\.wslconfig
    (or use netsh portproxy as the fallback).
  - Windows Firewall: allow inbound TCP on \$OSM_PORT.
  - Router: forward external TCP \$OSM_PORT → this machine's LAN IP.
  - DDNS updater: add a crontab entry per the runbook.
  - Fill in \$OSM_HOST / \$OSM_PORT in deployment-dev-host.md "Connection details".

Then index the Odoo sources (deployment-dev-host.md §3):
  cd $REPO_PATH
  for v in 17 18 19; do
    SHA=\$(git -C ~/git/\${v}.0/odoo rev-parse --short HEAD)
    DATABASE_URL="postgresql:///osm_\${v}" uv run python scripts/index.py \\
      --addons ~/git/\${v}.0/odoo/odoo/addons \\
      --addons ~/git/\${v}.0/odoo/addons \\
      --tenant public --git-sha "\$SHA"
  done

To authorize a teammate's key:
  scripts/osm-authorize.sh '<ssh-ed25519 AAAA… name@host>'

SUMMARY
