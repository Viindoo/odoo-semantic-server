# Disaster Recovery — Odoo Semantic MCP

Hướng dẫn phục hồi khi server mất data, DB corrupt, hoặc cần migrate sang host mới.

> **Bilingual note:** English headers; Vietnamese subnotes per project style.

---

## Backup Frequency Recommendations

| Thành phần | Tần suất khuyến nghị | Lý do |
|------------|---------------------|-------|
| **PostgreSQL** | Daily (hằng ngày) | Chứa profiles, repos registry, API keys, job history, embeddings — thay đổi thường xuyên |
| **Neo4j** | Weekly + on-demand trước major reindex | Graph data có thể rebuild từ source (xem §RTO), nhưng backup nhanh hơn re-index từ đầu |
| **Odoo source repos** | Không cần backup riêng | `git pull` là đủ — repos là read-only input, không có data duy nhất ở đây |
| **`webui.env` (FERNET_KEY)** | Ngay khi tạo lần đầu, và sau mỗi lần rotate | Mất key = không decrypt SSH private key — lưu vào secrets manager riêng biệt |
| **`odoo-semantic.conf`** | Khi thay đổi config | Chứa DB DSN + passwords — cùng secrets manager với webui.env |

### Automated PG backup (cron ví dụ)

```bash
# Chạy bằng user odoo-semantic, daily 2am:
sudo tee /etc/cron.d/odoo-semantic-pg-backup > /dev/null << 'EOF'
0 2 * * * odoo-semantic mkdir -p ~/backups && \
    docker exec odoo-semantic-mcp-postgres-1 pg_dump -U odoo_semantic odoo_semantic \
    > ~/backups/odoo_semantic_$(date +\%Y\%m\%d).sql 2>> /var/log/odoo-semantic-backup.log
# Giữ 14 ngày:
0 3 * * * odoo-semantic find ~/backups -name "odoo_semantic_*.sql" -mtime +14 -delete
EOF
```

### Automated Neo4j backup (weekly)

```bash
sudo tee -a /etc/cron.d/odoo-semantic-pg-backup > /dev/null << 'EOF'
# Neo4j dump mỗi Chủ nhật 3am:
0 3 * * 0 odoo-semantic mkdir -p ~/backups && \
    docker compose -f /opt/odoo-semantic-mcp/docker-compose.yml \
      exec neo4j sh -c 'mkdir -p /data/backups && neo4j-admin database dump neo4j --to-path=/data/backups' && \
    docker cp odoo-semantic-mcp-neo4j-1:/data/backups/neo4j.dump \
      ~/backups/neo4j-$(date +\%F).dump 2>> /var/log/odoo-semantic-backup.log
# Giữ 4 tuần:
0 4 * * 0 odoo-semantic find ~/backups -name "neo4j-*.dump" -mtime +28 -delete
EOF
```

---

## Restore Order

**Quan trọng: restore theo thứ tự này để tránh inconsistency.**

```
1. PostgreSQL (registry + auth + embeddings)
2. Neo4j (graph data — optional nếu sẽ re-index)
3. webui.env (FERNET_KEY — cần trước khi start Web UI)
4. Services restart
```

**Neo4j là optional step:** nếu dump bị corrupt hoặc không có, có thể bỏ qua bước 2 và re-index từ Odoo source repos (xem §RTO estimate). PostgreSQL chứa registry state (profiles/repos) đủ để re-index — không cần Neo4j dump.

---

## Step-by-Step Restore Commands

### 1. Restore PostgreSQL

```bash
# Dừng services trước khi restore (tránh write concurrent):
sudo systemctl stop odoo-semantic-mcp odoo-semantic-webui

# Restore từ dump:
docker compose exec -T postgres \
    psql -U odoo_semantic -d postgres \
    -c "DROP DATABASE IF EXISTS odoo_semantic; CREATE DATABASE odoo_semantic;"

docker compose exec -T postgres \
    psql -U odoo_semantic odoo_semantic \
    < ~/backups/odoo_semantic_<YYYYMMDD>.sql

# Verify:
docker compose exec postgres \
    psql -U odoo_semantic -c "SELECT COUNT(*) AS profiles FROM profiles;"
# → profiles > 0 nếu restore thành công
```

### 2. Restore Neo4j (optional)

```bash
# Neo4j phải stopped để load:
docker compose stop neo4j

# Copy dump vào container:
docker cp ~/backups/neo4j-<DATE>.dump \
    odoo-semantic-mcp-neo4j-1:/data/backups/

# Load (overwrite destination):
docker compose run --rm neo4j \
    neo4j-admin database load neo4j \
    --from-path=/data/backups \
    --overwrite-destination=true

# Khởi động lại:
docker compose start neo4j

# Verify — đếm Module nodes:
docker compose exec neo4j \
    cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
    "MATCH (m:Module) RETURN count(m) AS modules"
# Expected: modules > 0 (vd 100+ cho Odoo 17 base)
```

### 3. Restore webui.env

```bash
sudo install -o odoo-semantic -g odoo-semantic -m 600 /dev/null \
    /etc/odoo-semantic/webui.env
# Paste FERNET_KEY từ secrets manager:
echo "FERNET_KEY=<key từ secrets manager>" \
    | sudo tee /etc/odoo-semantic/webui.env > /dev/null
sudo chmod 600 /etc/odoo-semantic/webui.env
```

### 4. Restart services

```bash
sudo systemctl start odoo-semantic-webui odoo-semantic-mcp
sudo systemctl status odoo-semantic-mcp odoo-semantic-webui
# Verify health:
curl http://127.0.0.1:8002/health
# → {"neo4j": "ok", "postgres": "ok"}
```

---

## Validation Queries After Restore

**Chạy sau khi restore để xác nhận data integrity:**

```bash
# Neo4j — đếm graph nodes:
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "
MATCH (m:Module)    RETURN count(m) AS modules    UNION ALL
MATCH (mo:Model)    RETURN count(mo) AS models    UNION ALL
MATCH (f:Field)     RETURN count(f) AS fields     UNION ALL
MATCH (me:Method)   RETURN count(me) AS methods
"
# Expected (Odoo 17 base + viindoo addons, tham chiếu):
# modules ≥ 100 | models ≥ 200 | fields ≥ 5000 | methods ≥ 2000

# PostgreSQL — đếm embeddings + profiles:
docker compose exec postgres psql -U odoo_semantic -c "
SELECT 'profiles' AS table_name, COUNT(*) FROM profiles
UNION ALL
SELECT 'repos',                  COUNT(*) FROM repos
UNION ALL
SELECT 'api_keys',               COUNT(*) FROM api_keys
UNION ALL
SELECT 'embeddings',             COUNT(*) FROM embeddings;
"
# Expected: profiles ≥ 1, repos ≥ 1, api_keys ≥ 1
# embeddings = 0 nếu indexer chạy --no-embed; > 0 nếu có Ollama embed
```

---

## RTO Estimate

| Scenario | Estimated RTO | Lý do |
|----------|--------------|-------|
| **Full restore từ dump (PG + Neo4j)** | ~15–30 phút | PG restore nhanh; Neo4j dump load ~5 min cho 400 modules |
| **PG restore + Neo4j re-index từ source (no embed)** | ~45–90 phút | `index-repo --no-embed --all` với 400-600 modules ≈ 30-60 phút CPU-bound |
| **PG restore + Neo4j re-index từ source (full embed)** | ~3–6 giờ | Embedder là chokepoint — 46k chunks × 22s/100 texts qua remote Ollama |
| **Total loss, re-clone repos + full reindex** | ~4–8 giờ | Clone repo Odoo + addons: 30-60 min; reindex có embed: 3-6h |

**Recommend strategy:**
- Nếu Neo4j dump available và không corrupt → restore từ dump (~15 min) → **preferred**
- Nếu Neo4j dump missing/corrupt → restore PG → re-index `--no-embed` → service up fast, embed deferred
- Full embed có thể chạy background sau khi service đã up (idempotent, `setsid nohup`)

---

## Migration to New Host

Khi cần chuyển server (vd: scale up VM, DC migration):

```bash
# Trên host cũ — backup toàn bộ:
mkdir -p ~/backups
python -m src.cli backup --output ~/backups/pg-migration.sql
docker compose exec neo4j sh -c 'mkdir -p /data/backups && neo4j-admin database dump neo4j --to-path=/data/backups'
docker cp odoo-semantic-mcp-neo4j-1:/data/backups/neo4j.dump ~/backups/neo4j-migration.dump

# Copy sang host mới (rsync hoặc scp):
rsync -avz ~/backups/ <new-host>:~/backups/
# Cũng copy: /etc/odoo-semantic/ (config) và webui.env từ secrets manager

# Trên host mới — setup từ đầu (§1–§3.3 trong deploy.md), rồi restore theo order ở trên
```

---

## Post-Restore Behaviour

**Automatic graph consistency via head_sha mismatches (M7 W14).**

Sau khi restore, `repos.head_sha` trong PostgreSQL có thể không khớp với Neo4j graph
(ví dụ: Neo4j dump từ tuần trước, PG dump từ hôm qua — head_sha in PG refers to a
commit that graph reflects, but Neo4j has older state).

**Đây là intentional safety behaviour:** bất kỳ mismatch nào giữa PG `head_sha` và
Neo4j graph state sẽ trigger full reindex on next indexer run:

- Nếu `head_sha` trong PG khớp với current git HEAD → incremental skip → graph unchanged (stale Neo4j state persists).
- Nếu `head_sha` NULL (sau `reset_head_sha`) → full reindex → graph rebuilt from source.

**Recommended post-restore procedure:**

```bash
# Option A: Trust restored Neo4j dump + PG — no action needed if both from same backup window.
# Option B: Force full Neo4j graph rebuild from source (safest, slower):
python -m src.indexer index-repo --all --full
# → Ignores head_sha entirely, re-scans all modules, rebuilds all graph nodes/edges.

# Option C: Reset all head_sha to force re-index next scheduled run:
python -m src.db.migrate  # ensure schema up to date
psql -U odoo_semantic -c "UPDATE repos SET head_sha = NULL;"
# → Next cron run does full reindex per-repo automatically.
```

**Cross-repo propagation:** W14 dep-tracking also reset dependent repos' head_sha
during reindex. After restore, this propagation re-runs correctly on the next
incremental cycle — graph consistency is guaranteed across repos.

---

*Xem thêm: [docs/deploy.md §2.4](../deploy.md#24-backup-thủ-công) · [docs/deploy/pre-launch-checklist.md §5](pre-launch-checklist.md#5-backup--recovery)*
