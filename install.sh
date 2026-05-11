#!/usr/bin/env bash
# install.sh — Non-Docker installation for odoo-semantic-mcp
# Usage: bash install.sh [--systemd] [--help]
set -euo pipefail

VENV_PATH="$HOME/.venv/odoo-semantic-mcp"
CONFIG_DIR="$HOME/.odoo-semantic"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

show_help() {
    cat <<EOF
Usage: bash install.sh [OPTIONS]

Options:
  --systemd   Install systemd service files and print enable instructions
  --help      Show this help message

What this script does:
  1. Checks Python 3.12+
  2. Creates virtualenv at $VENV_PATH
  3. Installs project + dev dependencies
  4. Creates config directory $CONFIG_DIR
  5. Copies .env.example and odoo-semantic.conf.example if not present
  6. Prints FERNET_KEY generation instructions
  --systemd:  Installs systemd service files from docs/deploy/.
              Run as root for production paths (User=odoo-semantic,
              /opt/odoo-semantic-mcp).  Run as a regular user for dev
              workstation paths (auto-substituted to current user + cwd).
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
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "⚠ systemctl not found — skipping systemd unit installation (likely running in container or non-Linux host)"
        return 0
    fi

    local deploy_dir="$SCRIPT_DIR/docs/deploy"
    local target_dir="/etc/systemd/system"

    if [[ ! -d "$deploy_dir" ]]; then
        echo "✗ Service files not found at $deploy_dir" >&2
        exit 1
    fi

    # Detect run context: root → keep production paths; non-root → substitute for
    # the current user's dev workstation layout.
    local is_root=false
    [[ "$(id -u)" -eq 0 ]] && is_root=true

    local effective_user effective_workdir effective_venv effective_envfile
    if $is_root; then
        # Production: keep the canonical paths that ship in docs/deploy/
        effective_user="odoo-semantic"
        effective_workdir="/opt/odoo-semantic-mcp"
        effective_venv="/home/odoo-semantic/.venv/odoo-semantic-mcp"
        effective_envfile="/etc/odoo-semantic/webui.env"
    else
        # Dev workstation: substitute current user's paths
        effective_user="$(id -un)"
        effective_workdir="$SCRIPT_DIR"
        effective_venv="$HOME/.venv/odoo-semantic-mcp"
        effective_envfile="$SCRIPT_DIR/.env"
    fi

    echo ""
    echo "=== Systemd service install ==="
    if $is_root; then
        echo "  Mode: production (running as root)"
        echo "  User: $effective_user"
        echo "  WorkingDirectory: $effective_workdir"
    else
        echo "  Mode: dev workstation (running as $effective_user)"
        echo "  Paths auto-adjusted to current user layout"
        echo "  User: $effective_user"
        echo "  WorkingDirectory: $effective_workdir"
        echo ""
        echo "  ⚠  Writing to /etc/systemd/system requires sudo."
        echo "     Re-run this script with sudo if the copy step fails,"
        echo "     or copy manually and reload:"
        echo "     sudo cp /tmp/odoo-semantic-*.service /etc/systemd/system/"
        echo "     sudo systemctl daemon-reload"
    fi
    echo ""

    for svc_file in "$deploy_dir"/*.service; do
        local svc_name
        svc_name=$(basename "$svc_file")
        local tmp_file="/tmp/$svc_name"

        # Apply substitutions (sed -E for portability)
        sed \
            -e "s|User=odoo-semantic|User=$effective_user|g" \
            -e "s|WorkingDirectory=/opt/odoo-semantic-mcp|WorkingDirectory=$effective_workdir|g" \
            -e "s|/home/odoo-semantic/\.venv/odoo-semantic-mcp|$effective_venv|g" \
            -e "s|EnvironmentFile=-/etc/odoo-semantic/webui\.env|EnvironmentFile=-$effective_envfile|g" \
            "$svc_file" > "$tmp_file"

        echo "  Prepared: $tmp_file"
        if [[ -w "$target_dir" ]]; then
            cp "$tmp_file" "$target_dir/$svc_name"
            echo "  ✓ Installed: $target_dir/$svc_name"
        else
            echo "    (cannot write to $target_dir — copy manually or re-run with sudo)"
        fi
    done

    echo ""
    echo "Enable and start:"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now odoo-semantic-mcp.service"
    echo "  sudo systemctl enable --now odoo-semantic-webui.service"
    echo ""
    echo "Monitor:"
    echo "  sudo systemctl status odoo-semantic-mcp.service"
    echo "  sudo journalctl -u odoo-semantic-mcp.service -f"
}

main() {
    local do_systemd=false

    for arg in "$@"; do
        case "$arg" in
            --help) show_help; exit 0 ;;
            --systemd) do_systemd=true ;;
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

    # 6. Print FERNET_KEY instructions
    echo ""
    echo "=== Next steps ==="
    echo ""
    echo "1. Generate FERNET_KEY (required for SSH key storage):"
    echo "   $VENV_PATH/bin/python -c \\"
    echo "     \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo "   Add to $CONFIG_DIR/.env: FERNET_KEY=<generated-key>"
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
        install_systemd
    fi

    echo ""
    echo "✓ Installation complete"
}

main "$@"
