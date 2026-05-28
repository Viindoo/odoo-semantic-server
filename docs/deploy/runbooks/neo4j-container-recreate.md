# Neo4j Container Recreate Runbook

> Recreate the Neo4j Docker container from the canonical compose path
> (`/home/odoo-semantic/odoo-semantic-mcp/`). Use this runbook to fix
> `working_dir` label drift, pick up new env-vars (e.g.,
> `auth_max_failed_attempts=10` from TD-4 hardening), or apply an image bump.
> Data is preserved — named volumes are project-name-scoped, not path-scoped.
> ADR-0027.

---

## Nguyên Lý

Docker Compose labels (`com.docker.compose.project.working_dir`) are written at
container create time and are immutable thereafter. If a container was last
started from a developer's working tree (e.g.,
`/home/&lt;dev-user&gt;/git/odoo-semantic-server`) instead of the canonical system-
user path (`/home/odoo-semantic/odoo-semantic-mcp`), the label is permanently
wrong until the container is stopped, removed, and re-created.

**Volume safety:** Named volumes are scoped by project NAME
(`odoo-semantic-mcp`), not by working directory. The volume
`odoo-semantic-mcp_neo4j_data` persists across `docker compose rm` as long as
`--volumes` is not passed. Re-creating the container from any path that uses the
same project name will re-attach the same volume — **no reindex is needed**.

**Drift risk:** Running `docker compose up -d` from the canonical path while
the Neo4j container was started from a different path will cause a volume mount
conflict or port collision on 7474/7687 — because the canonical-path compose
project does not recognise the existing container as its own. This runbook
avoids that by explicitly stopping and removing the container from the path
where it was last started.

---

## Preconditions

- `docker-compose.yml` in the canonical path
  (`/home/odoo-semantic/odoo-semantic-mcp/`) has the desired env vars and image
  version active.
- Maintenance window (~30s Neo4j downtime) announced to active MCP users if
  any are online.
- No active indexer run in progress. Check:
  ```bash
  docker logs odoo-semantic-mcp-neo4j-1 --tail 5
  # Safe to proceed if no "checkpoint" or bulk-write activity in the last 60s
  ```
- Preferred window: after 04:30 ICT (nightly reindex at 03:30 ICT finishes and
  Neo4j goes idle).

---

## Placeholder Reference (ADR-0027 Canonical Defaults)

| Placeholder | Canonical default | Note |
|---|---|---|
| `<CANONICAL_PATH>` | `/home/odoo-semantic/odoo-semantic-mcp` | System-user canonical compose path |
| `<DEV_PATH>` | `/home/&lt;dev-user&gt;/git/odoo-semantic-server` | Developer path where container may have been started |
| `<APP_USER>` | `odoo-semantic` | System user running the compose stack |
| `<MCP_SERVICE>` | `odoo-semantic-mcp` | systemd unit for the MCP server |
| `<NEO4J_CONTAINER>` | `odoo-semantic-mcp-neo4j-1` | Docker container name (project name + service + instance) |

To find which path the Neo4j container was actually started from:

```bash
docker inspect odoo-semantic-mcp-neo4j-1 \
  -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

If the output is already `/home/odoo-semantic/odoo-semantic-mcp`, the container
is canonical — this runbook is a no-op for fixing drift, though it can still be
used to pick up env-var or image changes.

---

## Procedure

Recommended window: any day after 04:30 ICT.

```bash
# 1. Stop and remove Neo4j from the path where it was last up'd
#    (Check working_dir label above to confirm which path to use)
cd /home/&lt;dev-user&gt;/git/odoo-semantic-server
docker compose stop neo4j
docker compose rm -f neo4j
# NOTE: --volumes is NOT passed — named volume odoo-semantic-mcp_neo4j_data is preserved.
```

```bash
# 2. Re-create from canonical path
#    Compose creates a fresh container with correct working_dir label;
#    the named volume re-attaches automatically by project name.
cd /home/odoo-semantic/odoo-semantic-mcp
sudo -u odoo-semantic docker compose up -d --no-deps neo4j
```

```bash
# 3. Wait for Neo4j healthcheck to pass (max 60s)
#    The healthcheck in docker-compose.yml pings bolt://localhost:7687.
CONTAINER=odoo-semantic-mcp-neo4j-1
timeout 60 bash -c "
  while [ \"\$(docker inspect $CONTAINER -f '{{.State.Health.Status}}')\" != \"healthy\" ]; do
    echo 'Waiting for Neo4j healthcheck...'
    sleep 2
  done
"
echo "Neo4j is healthy"
```

```bash
# 4. Restart MCP service for a clean driver reconnect
#    The Neo4j Python driver may have stale connection-pool state after
#    the container restart — a service restart ensures a fresh pool.
sudo systemctl restart odoo-semantic-mcp
```

---

## Verification

### Label is canonical

```bash
docker inspect odoo-semantic-mcp-neo4j-1 \
  -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Expected: `/home/odoo-semantic/odoo-semantic-mcp`

### Volume is re-attached

```bash
docker inspect odoo-semantic-mcp-neo4j-1 \
  -f '{{range .Mounts}}{{.Name}} {{end}}'
```

Expected output contains `odoo-semantic-mcp_neo4j_data`.

### Env-vars are applied (example: auth_max_failed_attempts)

```bash
docker exec odoo-semantic-mcp-neo4j-1 env \
  | grep -E 'NEO4J_dbms_security_auth__max__failed'
```

Expected (if TD-4 hardening is in `docker-compose.yml`):
`NEO4J_dbms_security_auth__max__failed__attempts=10`

### MCP smoke

```bash
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:8002/health
```

Expected: `200`.

Run one MCP tool call to confirm the Neo4j driver reconnected:

```bash
curl -s \
  -X POST http://localhost:8002/mcp \
  -H "Authorization: Bearer <API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"list_available_versions","arguments":{}}}' \
  | jq '.result' | head -5
```

Expected: JSON result with version list (not an error).

### Both containers are under canonical path

From the canonical compose path, confirm compose now manages both services:

```bash
cd /home/odoo-semantic/odoo-semantic-mcp
sudo -u odoo-semantic docker compose ps
```

Expected: both `neo4j` and `postgres` entries with status `Up` and no
`(created from <dev-user-path>)` discrepancy.

---

## Rollback

If Neo4j fails to start from the canonical path (config error, image pull
failure, volume conflict), recreate it from the original dev path — the same
named volume re-attaches and data is intact:

```bash
cd /home/odoo-semantic/odoo-semantic-mcp
sudo -u odoo-semantic docker compose stop neo4j
sudo -u odoo-semantic docker compose rm -f neo4j

cd /home/&lt;dev-user&gt;/git/odoo-semantic-server
docker compose up -d neo4j

# Wait for healthy
CONTAINER=odoo-semantic-mcp-neo4j-1
timeout 60 bash -c "
  while [ \"\$(docker inspect $CONTAINER -f '{{.State.Health.Status}}')\" != \"healthy\" ]; do
    sleep 2
  done
"

# Restart MCP for clean reconnect
sudo systemctl restart odoo-semantic-mcp
```

Same data volume re-attaches in both directions. No reindex needed in either
direction.

---

## Background — Why This Drift Occurs

Two causes documented in the 2026-05-28 investigation:

1. **Pre-ADR-0027 personal-user layout:** Prior to the M13 system-user migration
   (ADR-0027), services ran from the developer's home directory. When Postgres
   was migrated to the canonical path but Neo4j was not explicitly recreated, the
   split-path state resulted.

2. **`docker compose up` from wrong path during ops:** If an operator runs
   `docker compose up neo4j` from the dev path during a maintenance window, the
   container is registered under that path. Future `docker compose` commands from
   the canonical path will not recognise it, silently managing only Postgres.

The root-cause fix is: always run `docker compose` commands from the canonical
path (`/home/odoo-semantic/odoo-semantic-mcp/`) for production operations.

---

## Related: Neo4j Auth Rate-Limit Burst (2026-05-26 RCA)

A burst of 11 simultaneous auth failures at 14:01:09 UTC was caused by
integration tests on the dev machine falling back to the production Neo4j
endpoint (`bolt://localhost:7687`) with the wrong password (`"password"`),
triggering `dbms.security.auth_max_failed_attempts` (default=3) after 3
failures.

**Hardening actions from the RCA:**

1. Raise `auth_max_failed_attempts` to 10 in `docker-compose.yml`:
   `NEO4J_dbms_security_auth__max__failed__attempts=10`
   → Pick up by running this runbook to recreate the container.

2. Kill any orphaned dev-tree MCP instance:
   `pkill -f "src.mcp.server"` or identify via `ps aux | grep "src.mcp"`.

3. Set `NEO4J_TEST_URI=bolt://localhost:57687` in `.env` on the dev machine to
   prevent test-fallback from targeting the production bolt port.

---

## References

- **`docs/adr/0027-system-user-deployment.md`** — canonical paths + system-user layout + `ProtectHome` gotcha
- **`docs/deploy/runbooks/post-pr-ops.md`** — post-PR deploy checklist (migrations, restarts, smoke)
- **Docker Compose project name scoping** — https://docs.docker.com/compose/project-name/
