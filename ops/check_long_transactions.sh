#!/usr/bin/env bash
# ops/check_long_transactions.sh
# Detect Neo4j transactions running longer than THRESHOLD_SECONDS (default: 300).
#
# Exits 0 if no long-running transactions detected.
# Exits 1 if any transaction exceeds the threshold (suitable for cron alerting).
# Exits 2 on connection or credential errors.
#
# USAGE:
#   ops/check_long_transactions.sh                  # uses env vars or docker-compose defaults
#   THRESHOLD_SECONDS=600 ops/check_long_transactions.sh
#   NEO4J_PASSWORD=secret ops/check_long_transactions.sh
#
# CREDENTIAL RESOLUTION (in order):
#   1. NEO4J_PASSWORD env var (bare password)
#   2. NEO4J_AUTH env var ("neo4j/<password>" format, same as docker-compose.yml)
#   3. Falls back to "password" (docker-compose.yml NEO4J_AUTH default)
#
# CONNECTION:
#   NEO4J_HOST  (default: localhost)
#   NEO4J_PORT  (default: 7687)
#   NEO4J_USER  (default: neo4j)
#
# CRON EXAMPLE (alert if any tx runs > 5 minutes):
#   */5 * * * * /opt/odoo-semantic-mcp/ops/check_long_transactions.sh >> /var/log/osm-zombie-check.log 2>&1

set -euo pipefail

# --- Config ---
THRESHOLD_SECONDS="${THRESHOLD_SECONDS:-300}"
NEO4J_HOST="${NEO4J_HOST:-localhost}"
NEO4J_PORT="${NEO4J_PORT:-7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CYPHER_FILE="${CYPHER_FILE:-${SCRIPT_DIR}/check_long_transactions.cypher}"

# --- Credential resolution ---
# Match docker-compose.yml: NEO4J_AUTH="neo4j/<pw>" is the canonical env var there.
# Support bare NEO4J_PASSWORD too so callers do not need to know the "neo4j/" prefix.
if [[ -n "${NEO4J_PASSWORD:-}" ]]; then
    _NEO4J_PW="${NEO4J_PASSWORD}"
elif [[ -n "${NEO4J_AUTH:-}" ]]; then
    # Strip "neo4j/" prefix: "${NEO4J_AUTH#neo4j/}" - matches healthcheck in docker-compose.yml
    _NEO4J_PW="${NEO4J_AUTH#neo4j/}"
else
    _NEO4J_PW="password"
fi

# --- Helpers ---
die() { echo "ERROR: $*" >&2; exit 2; }
ts()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# --- Pre-checks ---
# THRESHOLD_SECONDS is interpolated into a sed expression below; reject anything
# that is not a bare positive integer so a stray '/' or sed metachar can't break
# the substitution and exit non-zero (which a cron caller would misread as
# "zombie transactions detected", exit 1, rather than a config error, exit 2).
[[ "${THRESHOLD_SECONDS}" =~ ^[0-9]+$ ]] \
    || die "THRESHOLD_SECONDS must be a positive integer (got '${THRESHOLD_SECONDS}')."
[[ -f "${CYPHER_FILE}" ]] || die "Cypher file not found: ${CYPHER_FILE}"
command -v cypher-shell >/dev/null 2>&1 || die "cypher-shell not found in PATH. " \
    "Install Neo4j client tools or run inside the neo4j container: " \
    "docker exec -i odoo-semantic-mcp-neo4j-1 cypher-shell ..."

# --- Run query ---
echo "[$(ts)] Checking Neo4j for transactions running > ${THRESHOLD_SECONDS}s ..."

# Override the threshold in the Cypher by replacing the duration literal.
# The canonical Cypher uses duration('PT300S'); we substitute the user's threshold.
CYPHER_QUERY="$(sed "s/duration('PT300S')/duration('PT${THRESHOLD_SECONDS}S')/" "${CYPHER_FILE}")"

OUTPUT="$(echo "${CYPHER_QUERY}" | \
    cypher-shell \
        -a "bolt://${NEO4J_HOST}:${NEO4J_PORT}" \
        -u "${NEO4J_USER}" \
        -p "${_NEO4J_PW}" \
        --format plain \
    2>&1)" || {
    echo "ERROR: cypher-shell failed (exit $?). Is Neo4j reachable at bolt://${NEO4J_HOST}:${NEO4J_PORT}?" >&2
    echo "${OUTPUT}" >&2
    exit 2
}

# Count result rows (excluding header line).
# cypher-shell --format plain prints a header row then data rows.
ROW_COUNT="$(echo "${OUTPUT}" | tail -n +2 | grep -vc '^[[:space:]]*$' || true)"

if [[ "${ROW_COUNT}" -eq 0 ]]; then
    echo "[$(ts)] OK: no transactions running > ${THRESHOLD_SECONDS}s."
    exit 0
fi

# --- Alert ---
echo ""
echo "WARNING: ${ROW_COUNT} transaction(s) running > ${THRESHOLD_SECONDS}s detected!"
echo ""
echo "${OUTPUT}"
echo ""
echo "To terminate a specific transaction (Neo4j 5.x syntax):"
echo "  TERMINATE TRANSACTION '<transactionId>';"
echo ""
echo "To terminate multiple at once:"
echo "  CALL dbms.terminateTransactions(['<id1>', '<id2>']);"
echo ""
echo "See docs/operations/zombie-transactions.md for the full runbook."
echo ""
exit 1
