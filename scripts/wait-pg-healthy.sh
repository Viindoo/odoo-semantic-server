#!/usr/bin/env bash
# wait-pg-healthy.sh — poll `docker compose ps` until the postgres service is healthy.
#
# Why: after `docker compose up -d postgres`, the container is "running" almost
# immediately, but the database itself takes a few seconds to accept connections.
# `pg_isready` via `docker compose exec` is the canonical check, but bare loops
# with `sleep` are easy to get wrong (no timeout → hang on a broken container).
#
# Exits 0 on healthy, 1 on timeout. Default timeout 60s, override with PG_WAIT_TIMEOUT.
#
# Used by `make recreate-db` after the down→up cycle. Standalone usage:
#   PG_WAIT_TIMEOUT=120 bash scripts/wait-pg-healthy.sh
set -euo pipefail

TIMEOUT="${PG_WAIT_TIMEOUT:-60}"
SERVICE="${PG_SERVICE_NAME:-postgres}"
USER_DB="${PG_HEALTH_USER:-odoo_semantic}"

start=$(date +%s)
while true; do
    if docker compose exec -T "$SERVICE" pg_isready -U "$USER_DB" >/dev/null 2>&1; then
        echo "✓ postgres healthy"
        exit 0
    fi
    elapsed=$(( $(date +%s) - start ))
    if (( elapsed >= TIMEOUT )); then
        echo "✗ postgres not healthy after ${TIMEOUT}s — inspect with:" >&2
        echo "    docker compose ps" >&2
        echo "    docker compose logs --tail=50 ${SERVICE}" >&2
        exit 1
    fi
    printf "."
    sleep 2
done
