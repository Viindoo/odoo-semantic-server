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
  --systemd:  Copies systemd templates and prints enable instructions
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
    local systemd_dir="$SCRIPT_DIR/systemd"
    local target_dir="/etc/systemd/system"

    if [[ ! -d "$systemd_dir" ]]; then
        echo "✗ systemd templates not found at $systemd_dir" >&2
        exit 1
    fi

    echo ""
    echo "Systemd service templates:"
    echo "  Copy and customize before enabling:"
    echo ""
    for tmpl in "$systemd_dir"/*.service.template; do
        local svc_name
        svc_name=$(basename "$tmpl" .template)
        echo "  sudo cp $tmpl $target_dir/$svc_name"
        echo "  sudo nano $target_dir/$svc_name  # edit User and paths as needed"
    done
    echo ""
    echo "Then enable:"
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
