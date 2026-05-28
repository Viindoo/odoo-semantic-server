# Backup Confirm + DR Drill Runbook

> Confirm backup bundle bao gồm Neo4j component (PR #189 online Bolt export); chạy DR drill non-prod để đo RTO thực. ADR-0018, ADR-0019.

---

## Placeholder Reference (ADR-0027 Canonical Defaults)

| Placeholder | Canonical default | Note |
|---|---|---|
| `<MCP_SERVICE>` | `odoo-semantic-mcp` | systemd unit name for MCP server |
| `<WEBUI_SERVICE>` | `odoo-semantic-webui` | systemd unit name for FastAPI admin |
| `<BACKUP_SERVICE>` | `odoo-semantic-backup` | systemd unit name for nightly backup (oneshot) |
| `<DB_OWNER>` | `odoo_semantic` | Postgres role that owns the application database |
| `<DB_NAME>` | `odoo_semantic` | Postgres database name |
| `<BACKUP_DIR>` | `/var/backups/odoo-semantic` | Local directory where bundles are written |
| `<APP_USER>` | `odoo-semantic` | Unix user running Odoo Semantic MCP services |
| `<VENV_PATH>` | `/home/<APP_USER>/.venv/odoo-semantic-mcp/bin/python` | Path to Python executable in app venv |

Operators on non-canonical layouts substitute actual values throughout this runbook.

---

## Phần 1 — Backup Confirm (PR #189 Landing Verification)

### Nguyên lý

Trước PR #189, backup chỉ chứa `postgres.sql` (Neo4j phải offline để dump, không khả thi với running container). PR #189 ship **online Bolt export** — `neo4j-admin` được bỏ, thay bằng Neo4j Python driver qua Bolt protocol. Backup bundle **giờ phải chứa** file `neo4j.cypher` (hoặc `.dump` nếu fallback offline available).

**Rủi ro:** Silent missing component. Nightly timer chạy OK (exit code 0), backup lưu vào disk, manifest viết nhưng **neo4j.cypher không có** → restore bundle sẽ:
- `postgres.sql` restore OK, dữ liệu profiles/repos khôi phục
- Neo4j từ bundle: không có cypher → skip restore, Neo4j vẫn empty hoặc stale
- DR drill fail: "no patterns indexed", MCP tools trả "graph not ready"

**Gap to close:** Confirm `neo4j.cypher` landing tại prod trước restore rely vào nó.

### Commands

```bash
# 1. Trigger one-shot backup (via systemd — dùng exact env như nightly)
sudo systemctl start <BACKUP_SERVICE>

# 2. Wait + observe log
sleep 10
sudo journalctl -u <BACKUP_SERVICE> -n 20 --no-pager

# 3. Inspect bundle contents — kiểm tra 4 components
BACKUP_FILE=$(ls -t /var/backups/odoo-semantic/*.tar.gz 2>/dev/null | head -1)
if [ -z "$BACKUP_FILE" ]; then
  echo "ERROR: No backup found in /var/backups/odoo-semantic/"
  exit 1
fi

tar -tzf "$BACKUP_FILE" | sort
```

### Verify Checklist

- [ ] **tar list** chứa `postgres.sql.gz` (hoặc `.sql`)
- [ ] **tar list** chứa `neo4j.cypher` (online export, 4MB–500MB theo graph size)
- [ ] **tar list** chứa `fernet.enc` (nếu backup chạy với `--bundle-passphrase-env`; nightly default không có)
- [ ] **tar list** chứa `manifest.json`
- [ ] **manifest.json** (extract + jq) khai báo artifacts:
  ```bash
  tar -xOzf "$BACKUP_FILE" manifest.json | jq '.artifacts'
  # Expected: ["postgres.sql.gz", "neo4j.cypher", "manifest.json"]
  # hoặc ["postgres.sql.gz", "neo4j.cypher", "fernet.enc", "manifest.json"]
  ```

### Neo4j Component Size Check

```bash
# Verify neo4j.cypher non-trivial (>10KB — nếu empty dump sẽ <5KB)
NEO4J_BYTES=$(tar -tzvf "$BACKUP_FILE" | awk '/neo4j\.cypher/{print $3}')
echo "neo4j.cypher size: $NEO4J_BYTES bytes"
[ "$NEO4J_BYTES" -gt 10000 ] && echo "✓ OK" || echo "✗ FAIL: neo4j too small or missing"

# Optional: extract + check header
tar -xOzf "$BACKUP_FILE" neo4j.cypher | head -5
# Expect: "// neo4j.cypher — exported 2026-05-..."
```

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `neo4j.cypher` missing from tar | PR #189 not deployed on prod | Verify commit hash includes `e605db5` or later |
| Neo4j component <5KB | Empty graph or export skipped | Check `journalctl`: "NEO4J_PASSWORD not set" or "connection failed" |
| `manifest.json` doesn't list neo4j | Manifest outdated (ADR-0018 spec drift) | Verify `src/cli.py` `_cmd_backup` writes manifest correctly |
| Timer-driven backup differs from manual | Env var fallback vs LoadCredential difference | Both paths must read NEO4J_PASSWORD; verify `/etc/credstore/FERNET_KEY` exists |
| `postgres.sql` present, neo4j missing | Selective failure: neo4j export failed mid-stream | Check `journalctl` for "Neo4j export failed: ..." + driver traceback |

---

## Phần 2 — DR Drill (RTO Measurement & Validation)

### Nguyên lý

RTO trong [`docs/deploy/disaster-recovery.md`](../disaster-recovery.md) là **estimate tính toán** (15–30 min restore-only, 45–90 min full RTO), chưa **measured thực tế trên prod bundle**. Drill = restore prod backup vào non-prod environment riêng biệt (separate host hoặc isolated container set), đo real restore time + reindex time (nếu needed), cập nhật RTO numbers, validate data integrity.

**QUAN TRỌNG:** KHÔNG chạy drill trên prod. Restore là destructive (DETACH DELETE), dù có pre-restore safety backup.

### Precondition

- **Non-prod host:** same OS, Docker version, CPU/RAM comparable with prod (hoặc documented performance multiplier)
- **Fresh OSM stack** trên non-prod: `docker compose up -d` (postgres + neo4j + mcp + webui + backup — tất cả fresh, no data)
- **Recent prod backup** copied to non-prod: `/tmp/osm-drill/osm-backup-<date>.tar.gz`
- **FERNET_KEY credential:** copy từ prod `/etc/credstore/FERNET_KEY` → non-prod `/etc/credstore/FERNET_KEY` (root:root 0600)
  - Nếu credential không available, restore sẽ fail decrypting SSH keys (nếu bundle có `fernet.enc`)
  - Nightly bundle (no `fernet.enc`) không cần FERNET_KEY; DR bundle (disaster-recovery, có `fernet.enc`) cần
- **Stopwatch / time command ready**
- **Sufficient disk space:** 3× prod database size (one for dump, one for postgres, one for safety backup)

### Command Sequence

```bash
# === T0: START TIMER ===
T0=$(date +%s)
date -u "+T0 = %Y-%m-%d %H:%M:%S UTC"
echo "$T0" > /tmp/osm-drill-t0.txt

# === PRE-RESTORE: Stop services ===
# (Non-prod, so stopping is safe)
echo "=== Stopping services ==="
sudo systemctl stop <MCP_SERVICE> <WEBUI_SERVICE> 2>/dev/null || docker compose down -v

# === RESTORE BUNDLE ===
echo "=== Restoring bundle ==="
BUNDLE="/tmp/osm-drill/osm-backup-*.tar.gz"
if [ ! -f "$BUNDLE" ]; then
  echo "ERROR: Bundle not found at $BUNDLE"
  exit 1
fi

# Optional: set FERNET_KEY if bundle has fernet.enc
# export FERNET_KEY=$(cat /etc/credstore/FERNET_KEY 2>/dev/null || echo "")

sudo -u <APP_USER> <VENV_PATH> -m src.cli restore "$BUNDLE" --force

# === T1: CAPTURE RESTORE-ONLY TIME ===
T1=$(date +%s)
date -u "+T1 = %Y-%m-%d %H:%M:%S UTC"
echo "$T1" > /tmp/osm-drill-t1.txt
RESTORE_ONLY=$((T1 - T0))
echo "Restore-only time: $RESTORE_ONLY seconds = $(($RESTORE_ONLY / 60)) minutes"

# === POST-RESTORE: Restart services + wait healthy ===
echo "=== Restarting services ==="
sudo systemctl start <MCP_SERVICE> <WEBUI_SERVICE> 2>/dev/null || docker compose up -d

# === Wait for services ready (60s timeout) ===
echo "=== Waiting for services to be healthy ==="
HEALTH_URL="http://127.0.0.1:8002/health"
for i in $(seq 1 60); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null)
  if [ "$STATUS" = "200" ]; then
    echo "Health check passed"
    break
  fi
  echo "  Attempt $i/60: status=$STATUS, waiting..."
  sleep 5
done

# === T2: CAPTURE FULL RTO ===
T2=$(date +%s)
date -u "+T2 = %Y-%m-%d %H:%M:%S UTC"
echo "$T2" > /tmp/osm-drill-t2.txt
FULL_RTO=$((T2 - T0))
echo "Full RTO time: $FULL_RTO seconds = $(($FULL_RTO / 60)) minutes"

# === SMOKE TEST: MCP tool to verify data integrity ===
echo "=== Running MCP smoke tests ==="
DRILL_API_KEY="<DRILL_API_KEY>"  # Non-prod API key from admin UI
MCP_URL="http://127.0.0.1:8002"

# Smoke 1: model_inspect on sale.order
echo "Smoke 1: model_inspect(sale.order, method=summary)"
curl -X POST "$MCP_URL/mcp" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DRILL_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "model_inspect",
      "arguments": {"model": "sale.order", "method": "summary"}
    }
  }' 2>/dev/null | jq '.result.content[0].text' | head -10

# Smoke 2: find_examples (semantic search)
echo ""
echo "Smoke 2: find_examples('compute amount total')"
curl -X POST "$MCP_URL/mcp" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DRILL_API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
      "name": "find_examples",
      "arguments": {"query": "compute amount total", "limit": 1}
    }
  }' 2>/dev/null | jq '.result.content[0].text' | head -20

# === FINAL REPORT ===
echo ""
echo "========================================="
echo "DR DRILL REPORT"
echo "========================================="
echo "T0 (backup start):        $T0"
echo "T1 (restore finish):      $T1"
echo "T2 (services ready):      $T2"
echo "Restore-only time:        $RESTORE_ONLY seconds = $(($RESTORE_ONLY / 60))m $(($RESTORE_ONLY % 60))s"
echo "Full RTO (to healthy):    $FULL_RTO seconds = $(($FULL_RTO / 60))m $(($FULL_RTO % 60))s"
echo "Bundle file:              $(basename "$BUNDLE")"
echo "Bundle size:              $(du -h "$BUNDLE" | cut -f1)"
echo "Non-prod host:            $(hostname)"
echo "Date:                     $(date -u +%Y-%m-%d)"
echo "========================================="
```

### Data Integrity Checklist

After smoke tests pass:

```bash
# Check Postgres row counts match manifest expectations
echo "=== Postgres table counts ==="
docker compose exec postgres psql -U <DB_OWNER> -c "
SELECT tablename, n_live_tup FROM pg_stat_user_tables WHERE n_live_tup > 0 ORDER BY tablename;
"

# Check Neo4j node counts match manifest
echo "=== Neo4j node counts ==="
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "
MATCH (m:Module)  RETURN 'Module' AS node_type, count(m) AS count
UNION
MATCH (m:Model)   RETURN 'Model', count(m)
UNION
MATCH (f:Field)   RETURN 'Field', count(f)
UNION
MATCH (me:Method) RETURN 'Method', count(me)
ORDER BY node_type
"

# Verify manifest integrity (if bundle has manifest.json)
echo "=== Manifest check ==="
tar -xOzf "$BUNDLE" manifest.json | jq '{timestamp, odoo_versions, artifacts}'
```

- [ ] Postgres rows > 0 for `profiles`, `repos`, `api_keys`
- [ ] Neo4j modules ≥ 50 (baseline Odoo 17 core is ~100)
- [ ] Neo4j models ≥ 100
- [ ] MCP smoke tool returns structured tree (not error/empty)
- [ ] No "BROKEN" / "invalid" markers in smoke output
- [ ] Manifest timestamp matches bundle creation

### Report Template

Update `docs/deploy/disaster-recovery.md` section "RTO Estimate" (replace existing table row):

```markdown
## RTO Estimate (Measured 2026-05-<DD>)

| Scenario | Measured Time | Host | Bundle Size | Notes |
|----------|---------------|------|-------------|-------|
| **Full restore từ nightly bundle (PG + Neo4j cypher)** | <RESTORE_ONLY> min | <HOST> | <SIZE> MB | Restore-only (DB restore + cypher replay) |
| **Services ready + smoke pass (full RTO)** | <FULL_RTO> min | <HOST> | <SIZE> MB | From backup start to MCP healthy + model_inspect OK |
| **PG restore + Neo4j re-index (no embed)** | ~<ESTIMATE> min | <HOST> | — | If Neo4j cypher corrupt; re-index faster than full embed |

**Drill environment:**
- Host: `<HOSTNAME>` (instance type / vCPU / RAM)
- OS: `<OS_VERSION>`
- Docker version: `<DOCKER_VERSION>`
- Drill date: 2026-05-<DD>

**Data integrity post-restore:**
- Profiles: <COUNT> rows ✓
- Repos: <COUNT> rows ✓
- Neo4j Modules: <COUNT> nodes ✓
- Smoke `model_inspect(sale.order)`: OK ✓
- Smoke `find_examples(...)`: OK ✓

**Deviations from estimate:**
(If actual time differs from estimate in disaster-recovery.md, note reason here)
```

### Drill Checklist

- [ ] Non-prod host provisioned (fresh OSM stack, no data)
- [ ] Prod backup copied to non-prod
- [ ] FERNET_KEY credential copied (if bundle has `fernet.enc`)
- [ ] Services stopped before restore
- [ ] Restore command exits cleanly (exit code 0)
- [ ] Services restart + health check pass within 60s
- [ ] MCP smoke tools return non-empty structured response
- [ ] Postgres table counts > 0
- [ ] Neo4j node counts match manifest (within ±5%)
- [ ] manifest.json declares all 3–4 artifacts
- [ ] RTO report filed (update disaster-recovery.md)

---

## References

- [`docs/deploy/disaster-recovery.md`](../disaster-recovery.md) — RTO targets, restore order, validation queries
- [`docs/deploy/backup-runbook.md`](../backup-runbook.md) — Backup config, FERNET_KEY delivery, troubleshooting
- [`docs/adr/0018-backup-bundle-contract.md`](../../adr/0018-backup-bundle-contract.md) — Bundle contract (artifacts, manifest schema)
- [`docs/adr/0019-restore-upload-security.md`](../../adr/0019-restore-upload-security.md) — Restore security checklist, safety backup
- [`docs/adr/0020-fernet-key-delivery.md`](../../adr/0020-fernet-key-delivery.md) — FERNET_KEY lifecycle
- PR #189 · Neo4j online Bolt export (replace offline neo4j-admin dump)

---

## Drill Output Example

```
=========================================
DR DRILL REPORT
=========================================
T0 (backup start):        1716900000
T1 (restore finish):       1716900720  (720 seconds later = 12 minutes)
T2 (services ready):       1716901080  (1080 seconds = 18 minutes)
Restore-only time:        720 seconds = 12m 0s
Full RTO (to healthy):    1080 seconds = 18m 0s
Bundle file:              osm-backup-2026-05-28.tar.gz
Bundle size:              450 MB
Non-prod host:            osm-drill-01.local
Date:                     2026-05-28
=========================================
```

---

**Last updated:** 2026-05-28 (PR #189 online Bolt export merged)
