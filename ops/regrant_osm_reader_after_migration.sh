#!/usr/bin/env bash
# =============================================================================
# Purpose:      Re-grant osm_reader on all tables after any new migration that
#               adds tables requiring read-tier access for the MCP :8002 process.
#               Re-executes ops/rls_create_osm_reader.sql (idempotent) and then
#               reports the full current grant set for osm_reader.
# Phase:        Run after any migration (m13_006/007/008, or future migrations).
#               Use whenever "python -m src.db.migrate" adds a new table that
#               the MCP read tier needs to SELECT/INSERT.
# Inputs:       None (reads osm_reader password from /home/odoo-semantic/etc/mcp.env)
# Outputs:      Full grant report for osm_reader (grantee + table_name +
#               privilege_type, ordered by table_name + privilege_type), plus
#               a summary count. Appends to /tmp/osm-deploy-runlog.txt.
# Exit:         0 on success, 1 on failure (missing SQL file, container
#               unreachable, password extraction failure, psql error)
# Rollback:     Grants are additive; no rollback needed on partial success.
#               If a grant was incorrectly added, REVOKE it manually:
#               docker exec -i <container> psql -U odoo_semantic -d odoo_semantic \
#                   -c "REVOKE <privilege> ON TABLE <table> FROM osm_reader;"
# SAFE TO RE-RUN: yes — ops/rls_create_osm_reader.sql uses DO/IF NOT EXISTS +
#               ALTER ROLE + GRANT (all idempotent in Postgres).
# =============================================================================
set -euo pipefail

RUNLOG="/tmp/osm-deploy-runlog.txt"
PROD_ROOT="/home/odoo-semantic/odoo-semantic-mcp"
MCP_ENV="/home/odoo-semantic/etc/mcp.env"
PG_CONTAINER="odoo-semantic-mcp-postgres-1"
PG_USER="odoo_semantic"
PG_DB="odoo_semantic"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_FILE="${SQL_FILE:-${SCRIPT_DIR}/rls_create_osm_reader.sql}"

log() { echo "[$(date -u +%H:%M:%SZ)] $*" | tee -a "${RUNLOG}"; }

log "=== regrant_osm_reader_after_migration: start ==="

# Pre-checks
if [[ ! -f "${SQL_FILE}" ]]; then
  echo "FAIL: SQL file not found: ${SQL_FILE}" | tee -a "${RUNLOG}"
  exit 1
fi

if ! docker inspect "${PG_CONTAINER}" >/dev/null 2>&1; then
  echo "FAIL: postgres container '${PG_CONTAINER}' not found" | tee -a "${RUNLOG}"
  exit 1
fi

# Extract osm_reader password from mcp.env (accessible as odoo-semantic user).
# mcp.env contains the MCP DSN: PG_DSN=postgresql://osm_reader:<pass>@...
log "Extracting osm_reader password from ${MCP_ENV}"
OSM_PW=$(sudo -u odoo-semantic bash -lc "
  grep -oP '(?<=PG_DSN=postgresql://osm_reader:)[^@]+' ${MCP_ENV}
")

if [[ -z "${OSM_PW}" ]]; then
  echo "FAIL: could not extract osm_reader password from ${MCP_ENV}" | tee -a "${RUNLOG}"
  exit 1
fi
log "osm_reader password extracted (not logged)"

# Execute the idempotent rls_create_osm_reader.sql.
# sudo -u odoo-semantic reads the SQL file; docker exec gets the SQL on stdin.
log "Applying ${SQL_FILE}"
sudo -u odoo-semantic bash -c "
  cd '${PROD_ROOT}'
  docker exec -i '${PG_CONTAINER}' psql -U '${PG_USER}' -d '${PG_DB}' \
    -v ON_ERROR_STOP=1 \
    -v 'db_name=${PG_DB}' \
    -v 'osm_pw=${OSM_PW}' \
    -f - < '${SQL_FILE}'
" 2>&1 | tee -a "${RUNLOG}"
log "SQL applied OK"

# Report all current grants for osm_reader (across all tables, ordered).
# This gives a full picture after each run — no hardcoded expected count.
log "Current osm_reader grants (all tables):"
docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" -v ON_ERROR_STOP=1 -c "
SELECT grantee, table_name, privilege_type
  FROM information_schema.role_table_grants
 WHERE grantee = 'osm_reader'
 ORDER BY table_name, privilege_type;
" | tee -a "${RUNLOG}"

GRANT_COUNT=$(docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d "${PG_DB}" -tA -c "
SELECT count(*)
  FROM information_schema.role_table_grants
 WHERE grantee = 'osm_reader';
" | tr -d '[:space:]')

RESULT="OK: ${GRANT_COUNT} total grants for osm_reader (all tables)"
log "${RESULT}"
echo "${RESULT}"
