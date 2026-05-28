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
| **FERNET_KEY** (`/etc/credstore/FERNET_KEY`, root:root 0600 — systemd LoadCredential) | Ngay khi tạo lần đầu, và sau mỗi lần rotate | Mất key = không decrypt SSH private key / TOTP secret — lưu vào secrets manager riêng biệt |
| **`odoo-semantic.conf`** | Khi thay đổi config | Chứa DB DSN + passwords — cùng secrets manager với webui.env |

### Automated backup (cron ví dụ)

Từ W1A-1/W1A-2 (2026-05-28), canonical backup path là **`python -m src.cli backup`** — viết một
bundle tar.gz duy nhất chứa `postgres.dump` (pg_dump custom format, level-6 compression),
`neo4j.cypher` (Bolt export, không cần stop DB), và `manifest.json`. Cron pair cũ
(`pg_dump` plain + `neo4j-admin database dump`) đã được supersede.

```bash
# Chạy bằng user odoo-semantic, daily 2am:
sudo tee /etc/cron.d/odoo-semantic-backup > /dev/null << 'EOF'
0 2 * * * odoo-semantic /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
    -m src.cli backup \
    --output /var/backups/odoo-semantic/osm-$(date +\%Y\%m\%d-\%H\%M\%S).tar.gz \
    >> /var/log/odoo-semantic-backup.log 2>&1
EOF
```

> Retention (pruning) is handled automatically by `_prune_old_bundles` in `src/cli.py`
> (default: 14 bundles kept). See **§Retention policy** below.
>
> See `docs/adr/0018-backup-contract.md` for bundle spec.

### Retention policy

`_prune_old_bundles` (`src/cli.py`) deletes older bundles by mtime ascending after every
successful backup run. The bundle being written is never deleted.

| Parameter | Default | Override |
|-----------|---------|---------|
| Bundles kept (N most-recent) | 14 | `--keep-bundles N` (CLI) or `OSM_BACKUP_KEEP=N` (env) |
| Disable pruning entirely (not recommended) | — | `OSM_BACKUP_KEEP=999999` |

**Disk budget:** ~3 GB/bundle on current prod → retention 14 ≈ **~42 GB cap**.

CLI flag wins over env var. Mechanism is idempotent — safe to run multiple times.

> See `docs/adr/0018-backup-contract.md` for bundle spec.

---

## Restore Order

**Quan trọng: restore theo thứ tự này để tránh inconsistency.**

```
1. PostgreSQL (registry + auth + embeddings)
2. Neo4j (graph data — optional nếu sẽ re-index)
3. FERNET_KEY → /etc/credstore/FERNET_KEY (cần trước khi start Web UI + backup)
4. Services restart
```

**Neo4j là optional step:** nếu dump bị corrupt hoặc không có, có thể bỏ qua bước 2 và re-index từ Odoo source repos (xem §RTO estimate). PostgreSQL chứa registry state (profiles/repos) đủ để re-index — không cần Neo4j dump.

---

## Step-by-Step Restore Commands

### 1. Restore PostgreSQL

Restore is **format-aware** (auto-detected on filename extension by `python -m src.cli restore`):

- **New bundles (post-2026-05-28):** contain `postgres.dump` (pg_dump custom format) → `pg_restore` is used.
- **Legacy bundles:** contain `postgres.sql` (plain text) → `psql` is used (backwards-compat path).

**Single command handles both:**

```bash
python -m src.cli restore <bundle.tar.gz>
# handles both formats transparently; runs pre-restore safety backup first
```

**Manual restore (if needed outside the CLI):**

```bash
# Dừng services trước khi restore (tránh write concurrent):
sudo systemctl stop odoo-semantic-mcp odoo-semantic-webui

# Drop + recreate:
docker compose exec -T postgres \
    psql -U odoo_semantic -d postgres \
    -c "DROP DATABASE IF EXISTS odoo_semantic; CREATE DATABASE odoo_semantic;"

# New bundles (postgres.dump → pg_restore):
docker compose exec -T postgres \
    pg_restore -U odoo_semantic -d odoo_semantic /path/to/postgres.dump

# Legacy bundles (postgres.sql → psql):
docker compose exec -T postgres \
    psql -U odoo_semantic odoo_semantic \
    < ~/backups/odoo_semantic_<YYYYMMDD>.sql

# Verify:
docker compose exec postgres \
    psql -U odoo_semantic -c "SELECT COUNT(*) AS profiles FROM profiles;"
# → profiles > 0 nếu restore thành công
```

> See `docs/adr/0018-backup-contract.md` for bundle spec.

### 2. Restore Neo4j (optional)

**Preferred path (post-2026-05-28 bundles):** `python -m src.cli restore <bundle.tar.gz>`
khôi phục Neo4j tự động từ `neo4j.cypher` qua Bolt driver (không cần stop DB). Xem blockquote bên dưới.

**Legacy path (pre-2026-05-28 — old offline `neo4j-admin database dump` archives):**

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

> **Bundle restore (online `neo4j.cypher`):** Lệnh `python -m src.cli restore <bundle.tar.gz>`
> khôi phục Neo4j tự động từ `neo4j.cypher` qua Bolt driver (không cần stop DB).
> Restore này là **REPLACE, không phải merge** — nó chạy `MATCH (n) DETACH DELETE n`
> để xoá sạch graph hiện tại TRƯỚC khi replay các câu `CREATE`, vì replay lên graph
> non-empty sẽ nhân đôi mọi node/relationship (file dùng `CREATE`, không `MERGE`).
> Giữ đúng ngữ nghĩa destructive của offline `neo4j-admin database load` ở trên.
> Pre-restore safety backup trong lệnh `restore` chỉ chụp PostgreSQL; nếu cần
> rollback Neo4j hãy dùng bundle Neo4j gần nhất.

### 3. Restore FERNET_KEY

Production delivers the key via the **systemd credential store** (`LoadCredential=` trong
webui + backup units, per ADR-0020). Restore the **EXISTING** key (KHÔNG generate key mới —
SSH/TOTP secrets đã mã hóa bằng key đó) tới credstore path, root:root 0600:

```bash
# Primary (production) — khớp LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY:
sudo install -d -m 0700 -o root -g root /etc/credstore
printf '%s' "<FERNET_KEY từ secrets manager>" | sudo tee /etc/credstore/FERNET_KEY > /dev/null
sudo chmod 600 /etc/credstore/FERNET_KEY && sudo chown root:root /etc/credstore/FERNET_KEY
```

> ⚠️ Thiếu `/etc/credstore/FERNET_KEY` sẽ **hard-fail** webui + backup units ở
> status=243/CREDENTIALS (KHÔNG soft-fallback) — provision TRƯỚC khi start các service đó.
>
> **Dev / non-credstore fallback only:** `src.crypto` cũng đọc `$FERNET_KEY` từ env file
> `/home/odoo-semantic/etc/webui.env` (owner odoo-semantic, mode 600). Chỉ dùng path này
> trên host không dùng systemd LoadCredential.

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
- Nếu bundle (neo4j.cypher) available và không corrupt → `python -m src.cli restore <bundle>` (~15 min) → **preferred**
- Nếu bundle missing/corrupt → restore PG → re-index `--no-embed` → service up fast, embed deferred
- Full embed có thể chạy background sau khi service đã up (idempotent, `setsid nohup`)

---

## Migration to New Host

Khi cần chuyển server (vd: scale up VM, DC migration):

```bash
# Trên host cũ — backup toàn bộ (canonical bundle: postgres.dump + neo4j.cypher + manifest):
mkdir -p ~/backups
/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
    -m src.cli backup \
    --output ~/backups/osm-migration-$(date +%Y%m%d-%H%M%S).tar.gz

# Copy sang host mới (rsync hoặc scp):
rsync -avz ~/backups/ <new-host>:~/backups/
# Cũng copy: /etc/odoo-semantic/ (config) và webui.env từ secrets manager

# Trên host mới — setup từ đầu (§1–§3.3 trong deploy.md), rồi restore theo order ở trên:
python -m src.cli restore ~/backups/osm-migration-<TIMESTAMP>.tar.gz
```

### Re-point `repos.local_path` (ADR-0037 — bắt buộc khi checkout path đổi)

Stored file paths là **repo-relative** (ADR-0037), nên graph + embeddings vẫn hợp lệ
trên host mới — KHÔNG cần reindex chỉ vì đổi server. Cái DUY NHẤT cần cập nhật là
`repos.local_path` (anchor tuyệt đối tới checkout trên đĩa). Nếu đường dẫn checkout
trên host mới khác host cũ:

```bash
# Auto-clone repos: re-clone tự tính lại default_clone_dir theo $HOME mới + ghi local_path.
curl -X POST localhost:8003/api/repos/repos/<id>/clone   # cho từng repo, hoặc clone-all

# Manual repos (admin tự nhập path) — cập nhật trực tiếp tới checkout mới:
psql -U odoo_semantic -c "UPDATE repos SET local_path = '/new/path/to/<repo>' WHERE id = <id>;"

# Sau khi cập nhật local_path, restart MCP service để xoá in-memory resource cache
# (stylesheet resource reconstruct đọc local_path động mỗi lần serve):
sudo systemctl restart odoo-semantic-mcp
```

Verify: `resolve_stylesheet(...)` trả nội dung file OK (không "file unreadable on this server").
Không cần reindex trừ khi git HEAD cũng đổi (xem Post-Restore Behaviour bên dưới).

### Re-create osm_reader RLS role (cluster-global — KHÔNG nằm trong backup)

Postgres role là **cluster-global** → `pg_dump` (per-DB, file `postgres.dump` trong bundle)
**KHÔNG** chứa role `osm_reader`. Dump CÓ mang theo policy `embeddings_tenant` + `FORCE` + các
câu `GRANT ... TO osm_reader`, nhưng khi restore mà role chưa tồn tại thì các GRANT đó **báo
lỗi và bị bỏ** (restore không `ON_ERROR_STOP` nên vẫn chạy tiếp). Hệ quả trên host mới:
embeddings có FORCE+policy nhưng thiếu role + grants.

- Bật lại RLS enforcement: chạy **`sudo ops/rls_cutover.sh`** (idempotent — tạo lại role +
  grants + FORCE, ghi `mcp.env` với password MỚI, restart MCP, verify). Xem [deploy.md §3.8](../deploy.md).
- ⚠️ Nếu copy `mcp.env` cũ (trỏ osm_reader) sang host mới mà CHƯA tạo lại role → MCP không
  connect được. Tạo lại role TRƯỚC khi start MCP, hoặc bỏ `mcp.env` (MCP chạy owner DSN, RLS
  off — chấp nhận được nếu single-tenant) rồi cutover sau.
- Muốn mang theo cả role kèm password hash: `pg_dumpall --roles-only` trên host cũ — backup
  mặc định của OSM (per-DB) không làm việc này.

---

## Post-Restore Behaviour

**Automatic graph consistency via head_sha mismatches (M7 W14).**

Sau khi restore, `repos.head_sha` trong PostgreSQL có thể không khớp với Neo4j graph
(ví dụ: bundle Neo4j từ tuần trước, PG dump từ hôm qua — head_sha in PG refers to a
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
