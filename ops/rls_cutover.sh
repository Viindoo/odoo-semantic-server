#!/usr/bin/env bash
# ops/rls_cutover.sh — RLS read-tier enforcement cutover (ADR-0034 A5 / runbook §5.14).
#
# Idempotent + REUSABLE. Run on any of:
#   (a) the current prod server (enable RLS the first time),
#   (b) a fresh install (after `python -m src.db.migrate` has created the embeddings
#       policy), or
#   (c) a migrated host (the per-DB backup carries the policy + FORCE + grant statements
#       but NOT the osm_reader ROLE — roles are cluster-global, never in `pg_dump`; their
#       grants error out on restore. Re-run this to recreate role + grants + mcp.env).
#
# It: (1) creates/refreshes the non-owner `osm_reader` role + grants (via the canonical
# ops/rls_create_osm_reader.sql), (2) FORCEs RLS on `embeddings`, (3) points ONLY the MCP
# :8002 process at osm_reader by writing mcp.env (0600), (4) restarts MCP, (5) verifies,
# and rolls back (reverts MCP to the owner DSN) if verification fails.
#
# MUST run as root (systemctl + writes APP_USER files + docker exec):
#   sudo ops/rls_cutover.sh
#   sudo OSM_READER_PASSWORD='...' ops/rls_cutover.sh   # reuse an existing password
#
# Config (env overrides; defaults = ADR-0027 canonical prod layout):
set -uo pipefail

PG_CONTAINER="${PG_CONTAINER:-odoo-semantic-mcp-postgres-1}"
DB_OWNER="${DB_OWNER:-odoo_semantic}"
DB_NAME="${DB_NAME:-odoo_semantic}"
APP_USER="${APP_USER:-odoo-semantic}"
MCP_ENV="${MCP_ENV:-/home/odoo-semantic/etc/mcp.env}"
MCP_SERVICE="${MCP_SERVICE:-odoo-semantic-mcp}"
PG_DSN_HOST="${PG_DSN_HOST:-localhost}"
PG_DSN_PORT="${PG_DSN_PORT:-5432}"
HEALTH_URL="${HEALTH_URL:-http://localhost:8002/health}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL_FILE="${SQL_FILE:-$SCRIPT_DIR/rls_create_osm_reader.sql}"

die() { echo "FATAL: $*" >&2; exit 1; }
psql_owner() { docker exec -i "$PG_CONTAINER" psql -U "$DB_OWNER" -d "$DB_NAME" -v ON_ERROR_STOP=1 "$@"; }

[ "$(id -u)" -eq 0 ] || die "must run as root: sudo ops/rls_cutover.sh"
[ -f "$SQL_FILE" ] || die "ops SQL not found: $SQL_FILE"
docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || die "postgres container '$PG_CONTAINER' not found"
psql_owner -tAc "SELECT 1" >/dev/null 2>&1 || die "cannot connect to $DB_NAME as owner $DB_OWNER"
psql_owner -tAc "SELECT 1 FROM pg_policies WHERE tablename='embeddings' AND policyname='embeddings_tenant'" \
  | grep -q 1 || die "policy embeddings_tenant missing — run 'python -m src.db.migrate' first (m13_004)"

echo "== [0] baseline (owner bypasses RLS → sees full count) =="
BASE_COUNT="$(psql_owner -tAc 'SELECT count(*) FROM embeddings')"
echo "embeddings total (owner) = $BASE_COUNT"

PW="${OSM_READER_PASSWORD:-$(openssl rand -hex 24)}"
GENERATED=0; [ -z "${OSM_READER_PASSWORD:-}" ] && GENERATED=1

echo "== [1] create/refresh osm_reader role + grants (idempotent) =="
psql_owner -v osm_pw="$PW" -v db_name="$DB_NAME" -f - < "$SQL_FILE" >/dev/null || die "role/grant SQL failed"
echo "ok"

echo "== [2] FORCE ROW LEVEL SECURITY on embeddings (idempotent) =="
psql_owner -c "ALTER TABLE embeddings FORCE ROW LEVEL SECURITY;" >/dev/null || die "FORCE failed"
echo "ok"

echo "== [3] write MCP-only mcp.env ($MCP_ENV, 0600, owner $APP_USER) =="
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$(dirname "$MCP_ENV")"
( umask 077; cat > "$MCP_ENV" <<EOF
# MCP :8002 read-tier DSN — osm_reader (non-owner, RLS-enforced). ADR-0034 A5 / runbook §5.14.
# Written by ops/rls_cutover.sh. webui/backup/indexer do NOT load this file (keep owner DSN).
PG_DSN=postgresql://osm_reader:${PW}@${PG_DSN_HOST}:${PG_DSN_PORT}/${DB_NAME}
EOF
)
chown "$APP_USER:$APP_USER" "$MCP_ENV"; chmod 0600 "$MCP_ENV"
echo "ok"

echo "== [4] restart MCP =="
systemctl restart "$MCP_SERVICE"

echo "== [5] verify =="
ok=1
for i in $(seq 1 15); do curl -sf "$HEALTH_URL" >/dev/null 2>&1 && break; sleep 1; done
ACTIVE="$(systemctl is-active "$MCP_SERVICE" || true)"
HTOTAL="$(curl -s "$HEALTH_URL" | jq -r '.embeddings_total // "null"' 2>/dev/null || echo null)"
echo "service=$ACTIVE  /health.embeddings_total=$HTOTAL  (baseline=$BASE_COUNT)"
[ "$ACTIVE" = "active" ] || ok=0
[ "$HTOTAL" = "$BASE_COUNT" ] || { echo "WARN: /health total != baseline — GAP1 wrap or DSN issue"; ok=0; }

echo "-- grant sanity (expect all 't') --"
psql_owner -tAc "SELECT
  'emb_select='      ||has_table_privilege('osm_reader','embeddings','SELECT')||
  '  emb_NOwrite='   ||(NOT has_table_privilege('osm_reader','embeddings','INSERT'))||
  '  apikeys_select='||has_table_privilege('osm_reader','api_keys','SELECT')||
  '  session_insert='||has_table_privilege('osm_reader','api_key_session_state','INSERT')||
  '  not_super='      ||(NOT rolsuper)||'  not_bypassrls='||(NOT rolbypassrls)
  FROM pg_roles WHERE rolname='osm_reader'" || ok=0

echo "-- MCP connections now as osm_reader? --"
psql_owner -tAc "SELECT string_agg(DISTINCT usename, ',') FROM pg_stat_activity WHERE datname='$DB_NAME'"

mapfile -t PR < <(psql_owner -tAc "SELECT profile_name FROM embeddings WHERE profile_name IS NOT NULL GROUP BY profile_name ORDER BY count(*) DESC LIMIT 2")
if [ "${#PR[@]}" -ge 2 ]; then
  PA="${PR[0]}"; PB="${PR[1]}"
  echo "-- cross-tenant as osm_reader (GUC=$PA): rows of $PB expect 0, own expect >0 --"
  docker exec -i -e PGPASSWORD="$PW" "$PG_CONTAINER" psql -h "$PG_DSN_HOST" -U osm_reader -d "$DB_NAME" -tA <<SQL || ok=0
BEGIN;
SET LOCAL app.allowed_profiles = '$PA';
SELECT 'cross_tenant(expect 0)=' || count(*) FROM embeddings WHERE profile_name = '$PB';
SELECT 'own(expect >0)='        || count(*) FROM embeddings WHERE profile_name = '$PA';
ROLLBACK;
SQL
else
  echo "-- cross-tenant smoke skipped: <2 profiles with data (fresh install) --"
fi

if [ "$ok" -ne 1 ]; then
  echo; echo "!! VERIFICATION FAILED — rolling back (revert MCP to owner DSN) !!"
  rm -f "$MCP_ENV"
  systemctl restart "$MCP_SERVICE"
  echo "rollback done: mcp.env removed, MCP back on owner DSN."
  echo "(osm_reader role + FORCE left in place — harmless; fix the issue and re-run.)"
  exit 1
fi

echo; echo "== DONE — RLS enforcement ACTIVE for MCP :8002 (osm_reader, FORCE) =="
if [ "$GENERATED" -eq 1 ]; then
  echo "osm_reader password (STORE in secrets manager alongside FERNET; also lives in $MCP_ENV):"
  echo "    $PW"
fi
echo "Rollback: rm $MCP_ENV && systemctl restart $MCP_SERVICE"
echo "          (optional) docker exec $PG_CONTAINER psql -U $DB_OWNER -d $DB_NAME -c 'ALTER TABLE embeddings NO FORCE ROW LEVEL SECURITY;'"
