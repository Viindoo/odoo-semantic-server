#!/usr/bin/env sh
# Bootstrap the dev environment. Idempotent.
set -eu

if ! command -v uv >/dev/null 2>&1; then
    echo "[bootstrap] uv not found on PATH."
    echo "[bootstrap] Install with:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "[bootstrap] Then re-run: bash scripts/bootstrap.sh"
    exit 1
fi

echo "[bootstrap] uv found: $(uv --version)"
echo "[bootstrap] Syncing dependencies (runtime + dev)..."
uv sync --extra dev

echo
echo "[bootstrap] Done. Next steps:"
echo "  1. Copy .env.example to .env and fill in secrets."
echo "  2. Start Postgres:         docker compose up -d db"
echo "  3. Apply migrations:       make migrate"
echo "  4. Run tests:              make test"
