#!/usr/bin/env bash
# install.sh — Non-Docker installation for odoo-semantic-mcp
# Usage: bash install.sh [--systemd] [--force-overrides] [--with-overrides] [--help]
set -euo pipefail

VENV_PATH="$HOME/.venv/odoo-semantic-mcp"
CONFIG_DIR="$HOME/.odoo-semantic"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_help() {
    cat <<EOF
Usage: bash install.sh [OPTIONS]

Options:
  --systemd            Install systemd service files and print enable instructions
  --force-overrides    Overwrite installed unit bodies even if they diverge from the
                       shipped template (default: warn + skip — see issue #144).
  --with-overrides     Also install drop-in override examples from
                       docs/deploy/overrides/ to /etc/systemd/system/<unit>.service.d/
                       (renamed to .conf, ready to edit).
  --help               Show this help message

What this script does:
  1. Checks Python 3.12+
  2. Creates virtualenv at $VENV_PATH
  3. Installs project + dev dependencies
  4. Creates config directory $CONFIG_DIR
  5. Copies .env.example and odoo-semantic.conf.example if not present
  6. Prints FERNET_KEY generation instructions
  --systemd:  Installs systemd service files from docs/deploy/.
              Default behavior is IDEMPOTENT: if a unit body already exists at
              /etc/systemd/system/<unit>.service and diverges from the shipped
              template, this script SKIPS it (rather than silently overwriting
              operator customizations as the bare \`cp\` pattern did pre-#144).
              Use --force-overrides to override this safety check.
              Use --with-overrides to lay down drop-in override scaffolding.
              Run as root for production paths (User=odoo-semantic,
              /home/odoo-semantic/odoo-semantic-mcp).  Run as a regular user
              for dev workstation paths (auto-substituted to current user + cwd).
EOF
}

check_python() {
    local python_cmd
    if command -v python3.12 &>/dev/null; then
        python_cmd="python3.12"
    elif command -v python3 &>/dev/null; then
        python_cmd="python3"
    else
        echo "✗ Python 3 not found. Install Python 3.12+." >&2
        exit 1
    fi

    local version
    version=$($python_cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') || {
        echo "✗ Failed to get Python version" >&2
        exit 1
    }
    local major minor
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)
    if [[ "$major" -lt 3 ]] || [[ "$major" -eq 3 && "$minor" -lt 12 ]]; then
        echo "✗ Python 3.12+ required (found $version). Please upgrade." >&2
        exit 1
    fi
    echo "$python_cmd"
}

install_systemd() {
    local force_overrides="${1:-false}"
    local with_overrides="${2:-false}"

    if ! command -v systemctl >/dev/null 2>&1; then
        echo "⚠ systemctl not found — skipping systemd unit installation (likely running in container or non-Linux host)"
        return 0
    fi

    local deploy_dir="$SCRIPT_DIR/docs/deploy"
    local overrides_dir="$SCRIPT_DIR/docs/deploy/overrides"
    local target_dir="/etc/systemd/system"

    if [[ ! -d "$deploy_dir" ]]; then
        echo "✗ Service files not found at $deploy_dir" >&2
        exit 1
    fi

    # Detect run context: root → keep ADR-0027 canonical paths; non-root → substitute
    # for the current user's dev workstation layout.
    local is_root=false
    [[ "$(id -u)" -eq 0 ]] && is_root=true

    local effective_user effective_group effective_workdir effective_venv
    local effective_configdir effective_reposdir
    if $is_root; then
        # Production: ADR-0027 canonical layout (system user + /home/odoo-semantic/)
        effective_user="odoo-semantic"
        effective_group="odoo-semantic"
        effective_workdir="/home/odoo-semantic/odoo-semantic-mcp"
        effective_venv="/home/odoo-semantic/.venv/odoo-semantic-mcp"
        effective_configdir="/home/odoo-semantic/etc"
        effective_reposdir="/home/odoo-semantic/repos"
    else
        # Dev workstation: substitute current user's paths
        effective_user="$(id -un)"
        effective_group="$(id -gn)"
        effective_workdir="$SCRIPT_DIR"
        effective_venv="$HOME/.venv/odoo-semantic-mcp"
        effective_configdir="$CONFIG_DIR"
        effective_reposdir="$HOME/repos"
    fi

    echo ""
    echo "=== Systemd service install ==="
    if $is_root; then
        echo "  Mode: production (running as root) — ADR-0027 canonical layout"
    else
        echo "  Mode: dev workstation (running as $effective_user)"
        echo "  Paths auto-adjusted to current user layout"
    fi
    echo "  User:             $effective_user"
    echo "  Group:            $effective_group"
    echo "  WorkingDirectory: $effective_workdir"
    echo "  Venv:             $effective_venv"
    echo "  Config dir:       $effective_configdir"
    echo "  Repos dir:        $effective_reposdir"
    if $force_overrides; then
        echo "  --force-overrides: WILL overwrite divergent installed units"
    else
        echo "  Idempotent mode:   diverged units SKIPPED (use --force-overrides to override)"
    fi
    if $with_overrides; then
        echo "  --with-overrides:  drop-in override scaffolding will be installed"
    fi
    if ! $is_root; then
        echo ""
        echo "  ⚠  Writing to /etc/systemd/system requires sudo."
        echo "     Re-run this script with sudo if the copy step fails,"
        echo "     or copy manually and reload:"
        echo "     sudo cp /tmp/odoo-semantic-*.service /etc/systemd/system/"
        echo "     sudo systemctl daemon-reload"
    fi
    echo ""

    # Separate per-outcome counters so the summary line never lumps
    # categorically-different outcomes together (issue #145 review):
    #   in_sync           — file existed AND body matched prepared template
    #   new_written       — file did not exist AND cp succeeded
    #   force_overwritten — divergent body replaced via --force-overrides
    #   skipped_divergent — divergent body left alone (no --force-overrides)
    #   pending_write     — would have written but $target_dir not writable
    local in_sync_count=0 new_written_count=0 force_overwritten_count=0
    local skipped_divergent_count=0 pending_write_count=0

    # Install both .service units AND .timer units so install.sh is the One
    # True Install Path (issue #145 review — .timer files were previously left
    # to manual `cp` per m9-postmerge-ops.md §6). Timers carry no substitutable
    # paths, so the sed pass below is a harmless no-op for them; the idempotency
    # check + counters apply uniformly.
    for svc_file in "$deploy_dir"/*.service "$deploy_dir"/*.timer; do
        # Guard against a non-matching glob expanding to the literal pattern
        # (nullglob is not set here).
        [[ -f "$svc_file" ]] || continue
        local svc_name
        svc_name=$(basename "$svc_file")
        local tmp_file="/tmp/$svc_name"
        local target_file="$target_dir/$svc_name"
        # Track this iteration's pending write category. Set by the
        # divergence/new-file branches below; consumed by the cp block.
        local pending_kind=""

        # Apply substitutions for non-canonical layouts (no-op when run as root
        # with canonical paths). Order matters: the more specific `.venv/...`
        # path is substituted BEFORE the broader `odoo-semantic-mcp` path so the
        # venv pattern doesn't get clobbered. Then user, group, repos dir, and
        # config dir each get their own pass.
        sed \
            -e "s|/home/odoo-semantic/\.venv/odoo-semantic-mcp|$effective_venv|g" \
            -e "s|/home/odoo-semantic/odoo-semantic-mcp|$effective_workdir|g" \
            -e "s|/home/odoo-semantic/repos|$effective_reposdir|g" \
            -e "s|/home/odoo-semantic/etc|$effective_configdir|g" \
            -e "s|^User=odoo-semantic$|User=$effective_user|g" \
            -e "s|^Group=odoo-semantic$|Group=$effective_group|g" \
            "$svc_file" > "$tmp_file"

        echo "  Prepared: $tmp_file"

        # Idempotency check: compare installed vs prepared. If body matches,
        # no-op. If body diverges, warn + skip unless --force-overrides.
        if [[ -f "$target_file" ]]; then
            if cmp -s "$tmp_file" "$target_file"; then
                echo "    ✓ Already in sync: $target_file (no-op)"
                ((in_sync_count++)) || true
                continue
            fi
            echo "    ⚠  Installed body diverges from prepared template:"
            diff -u "$target_file" "$tmp_file" | head -40 || true
            if ! $force_overrides; then
                echo "    ✗ SKIPPED (use --force-overrides to overwrite, or move"
                echo "      operator customizations into a drop-in override at"
                echo "      $target_dir/${svc_name}.d/ — see docs/deploy/install-runbook.md)"
                ((skipped_divergent_count++)) || true
                continue
            fi
            echo "    → Overwriting (--force-overrides)"
            pending_kind="force_overwritten"
        else
            pending_kind="new"
        fi

        if [[ -w "$target_dir" ]]; then
            cp "$tmp_file" "$target_file"
            echo "    ✓ Installed: $target_file"
            if [[ "$pending_kind" == "force_overwritten" ]]; then
                ((force_overwritten_count++)) || true
            else
                ((new_written_count++)) || true
            fi
        else
            echo "    (cannot write to $target_dir — copy manually or re-run with sudo)"
            ((pending_write_count++)) || true
        fi
    done

    if $with_overrides && [[ -d "$overrides_dir" ]]; then
        echo ""
        echo "=== Installing drop-in override scaffolding ==="
        for unit_d in "$overrides_dir"/*.service.d; do
            [[ -d "$unit_d" ]] || continue
            local unit_d_name
            unit_d_name=$(basename "$unit_d")
            local dst_unit_d="$target_dir/$unit_d_name"
            if [[ -w "$target_dir" ]]; then
                mkdir -p "$dst_unit_d"
                for ex in "$unit_d"/*.example; do
                    [[ -f "$ex" ]] || continue
                    local dst_file="$dst_unit_d/$(basename "${ex%.example}")"
                    if [[ -f "$dst_file" ]]; then
                        echo "  ✓ $dst_file already exists (keeping operator copy)"
                    else
                        cp "$ex" "$dst_file"
                        echo "  ✓ Installed scaffold: $dst_file (edit before reload)"
                    fi
                done
            else
                echo "  (cannot write to $target_dir — re-run with sudo)"
            fi
        done
    fi

    echo ""
    echo "Summary:"
    echo "  $new_written_count newly installed"
    echo "  $in_sync_count already in sync (no-op)"
    echo "  $force_overwritten_count force-overwritten (--force-overrides)"
    echo "  $skipped_divergent_count skipped (divergent — kept operator customizations)"
    if [[ $pending_write_count -gt 0 ]]; then
        echo "  $pending_write_count pending write (target dir not writable — re-run with sudo)"
    fi
    if [[ $skipped_divergent_count -gt 0 ]]; then
        echo ""
        echo "⚠  $skipped_divergent_count unit(s) skipped. Recommended next step:"
        echo "   1. Run \`make check-systemd-overrides\` to inspect the drift."
        echo "   2. Move operator customizations into drop-in overrides at"
        echo "      $target_dir/<unit>.service.d/  (see docs/deploy/overrides/)."
        echo "   3. Re-run \`bash install.sh --systemd\` — divergence should clear."
    fi

    echo ""
    echo "Enable and start:"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now odoo-semantic-mcp.service"
    echo "  sudo systemctl enable --now odoo-semantic-webui.service"
    echo "  sudo systemctl enable --now odoo-semantic-astro.service"
    echo "  # Timers (oneshot services fire via these — enable the .timer, not the .service):"
    echo "  sudo systemctl enable --now odoo-semantic-backup.timer"
    echo "  sudo systemctl enable --now osm-ttl-cleanup.timer"
    echo ""
    echo "Monitor:"
    echo "  sudo systemctl status odoo-semantic-mcp.service"
    echo "  sudo journalctl -u odoo-semantic-mcp.service -f"
    echo "  systemctl list-timers 'odoo-semantic-*' 'osm-*'"
}

main() {
    local do_systemd=false
    local force_overrides=false
    local with_overrides=false

    for arg in "$@"; do
        case "$arg" in
            --help) show_help; exit 0 ;;
            --systemd) do_systemd=true ;;
            --force-overrides) force_overrides=true ;;
            --with-overrides) with_overrides=true ;;
            *) echo "Unknown option: $arg" >&2; show_help; exit 1 ;;
        esac
    done

    echo "=== odoo-semantic-mcp installer ==="
    echo ""

    # 1. Check Python
    local python_cmd
    python_cmd=$(check_python)
    local version
    version=$($python_cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') || true
    echo "✓ Python $version"

    # 2. Create venv
    if [[ ! -d "$VENV_PATH" ]]; then
        echo "Creating virtualenv at $VENV_PATH..."
        $python_cmd -m venv "$VENV_PATH"
        echo "✓ Virtualenv created"
    else
        echo "✓ Virtualenv exists at $VENV_PATH"
    fi

    # 3. Install dependencies
    echo "Installing project dependencies..."
    "$VENV_PATH/bin/pip" install --quiet --upgrade pip
    "$VENV_PATH/bin/pip" install --quiet -e "$SCRIPT_DIR[dev]"
    echo "✓ Dependencies installed"

    # 4. Create config dir
    mkdir -p "$CONFIG_DIR"
    echo "✓ Config directory: $CONFIG_DIR"

    # 5. Copy config templates if not present
    if [[ -f "$SCRIPT_DIR/.env.example" && ! -f "$CONFIG_DIR/.env" ]]; then
        cp "$SCRIPT_DIR/.env.example" "$CONFIG_DIR/.env"
        echo "✓ Copied .env.example → $CONFIG_DIR/.env (edit to fill in passwords)"
    fi
    # Also ensure a repo-local .env exists so systemd dev units (EnvironmentFile=-$SCRIPT_DIR/.env)
    # and docker compose both find the file in the expected location.
    if [[ -f "$SCRIPT_DIR/.env.example" && ! -f "$SCRIPT_DIR/.env" ]]; then
        cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
        echo "✓ Copied .env.example → $SCRIPT_DIR/.env (repo-local, for docker compose + dev systemd units)"
    fi
    if [[ -f "$SCRIPT_DIR/odoo-semantic.conf.example" && ! -f "$CONFIG_DIR/odoo-semantic.conf" ]]; then
        cp "$SCRIPT_DIR/odoo-semantic.conf.example" "$CONFIG_DIR/odoo-semantic.conf"
        echo "✓ Copied odoo-semantic.conf.example → $CONFIG_DIR/odoo-semantic.conf"
    fi
    # Provision a dev webui.env with real generated secrets so the Web UI does
    # NOT silently fall back to dev-mode defaults (FERNET_KEY unset → SSH key
    # features disabled; WEBUI_SESSION_SECRET regenerated each restart → sessions
    # invalidated). The dev systemd unit's second EnvironmentFile= resolves to
    # $CONFIG_DIR/webui.env (the `/home/odoo-semantic/etc` → $CONFIG_DIR
    # substitution in install_systemd); without this file that line is a dead
    # path. mode 600 — these are secrets. (issue #145 review #5)
    if [[ ! -f "$CONFIG_DIR/webui.env" ]]; then
        local fernet_key session_secret
        fernet_key=$("$VENV_PATH/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null) || fernet_key=""
        session_secret=$("$VENV_PATH/bin/python" -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null) || session_secret=""
        if [[ -n "$fernet_key" && -n "$session_secret" ]]; then
            ( umask 077; printf 'FERNET_KEY=%s\nWEBUI_SESSION_SECRET=%s\n' \
                "$fernet_key" "$session_secret" > "$CONFIG_DIR/webui.env" )
            echo "✓ Generated $CONFIG_DIR/webui.env (FERNET_KEY + WEBUI_SESSION_SECRET, mode 600)"
        else
            echo "⚠ Could not generate $CONFIG_DIR/webui.env (cryptography unavailable?) — Web UI will run in dev-mode fallback until you create it"
        fi
    fi

    # 6. Print next-step instructions
    echo ""
    echo "=== Next steps ==="
    echo ""
    echo "1. FERNET_KEY + WEBUI_SESSION_SECRET were auto-generated into"
    echo "   $CONFIG_DIR/webui.env (mode 600). To rotate, delete that file and"
    echo "   re-run install.sh, or regenerate manually:"
    echo "   $VENV_PATH/bin/python -c \\"
    echo "     \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo ""
    echo "2. Set passwords in $CONFIG_DIR/.env:"
    echo "   NEO4J_PASSWORD=<password>"
    echo "   PG_PASSWORD=<password>"
    echo "   PG_DSN=postgresql://odoo_semantic:<password>@localhost:5432/odoo_semantic"
    echo ""
    echo "3. Start databases: docker compose up -d"
    echo "4. Run migrations: $VENV_PATH/bin/python -m src.db.migrate"
    echo "5. Start MCP server: $VENV_PATH/bin/python -m src.mcp.server"
    echo "6. Start Web UI: $VENV_PATH/bin/python -m src.web_ui  (port 8003)"

    if $do_systemd; then
        install_systemd "$force_overrides" "$with_overrides"
    fi

    echo ""
    echo "✓ Installation complete"
}

main "$@"
