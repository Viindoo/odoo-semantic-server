# Production Deploy — Odoo Semantic MCP

Hướng dẫn này dành cho **admin** deploy server. Developer xem [`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## 0. Topology

```
Người dùng (AI tool)
        │ HTTPS :443
        ▼
  ┌─────────────┐
  │  Nginx/Caddy │  ← reverse proxy, TLS termination, auth
  └──────┬──────┘
         │ HTTP 127.0.0.1:8002
         ▼
  ┌─────────────┐
  │  MCP Server  │  ← python -m src.mcp.server (systemd)
  └──────┬───┬──┘
         │   │
   bolt  │   │ psycopg2
7687 ▼   │   ▼ 5432
  ┌──────┴───┴──┐
  │  Databases   │  ← docker compose (Neo4j + PostgreSQL)
  └─────────────┘
```

**Same-server (default, ≤30 users):** tất cả tiers trên 1 host.
**Split-tier (≥80 users / HA):** DB trên VM riêng — xem [§8 Split-Tier](#8-split-tier-migration).

---

## 1. Prerequisites

| Thứ | Phiên bản | Dùng cho |
|-----|-----------|---------|
| Ubuntu 24.04 LTS | Noble | OS khuyến nghị |
| Docker Engine | 24+ | DB tier |
| Python | 3.12 | App tier |
| uv | 0.4+ | Package manager |
| Nginx **hoặc** Caddy | bất kỳ | Proxy tier |
| DNS record | — | Trỏ domain về IP server |
| TLS cert | — | Let's Encrypt hoặc wildcard |

### 1.1 Linux User & Group

Tạo system user/group **trước** khi setup DB và App tier — cần cho `chown` config file (§3.2) và systemd service (§3.5).

```bash
# Tạo group trước để kiểm soát GID
sudo groupadd --system odoo-semantic

# Tạo system user: nologin shell + có home dir cho venv.
#   - Home dir bắt buộc: venv ở /home/odoo-semantic/.venv/odoo-semantic-mcp/
#     consistent với Makefile ($HOME/.venv) + odoo-semantic-mcp.service
#     (ExecStart trỏ /home/odoo-semantic/.venv/...).
#   - Shell /usr/sbin/nologin vẫn chặn login interactive; -m chỉ tạo dir.
sudo useradd \
    --system \
    --create-home \
    --home-dir /home/odoo-semantic \
    --shell /usr/sbin/nologin \
    --gid odoo-semantic \
    odoo-semantic
```

Xác nhận:
```bash
id odoo-semantic
# uid=999(odoo-semantic) gid=999(odoo-semantic) groups=999(odoo-semantic)
ls -ld /home/odoo-semantic
# drwxr-x--- 2 odoo-semantic odoo-semantic ... (home tồn tại, owner đúng)
```

> **Recovery (nếu user đã tạo trước với `--no-create-home`):**
> ```bash
> sudo mkdir -p /home/odoo-semantic
> sudo chown -R odoo-semantic:odoo-semantic /home/odoo-semantic
> sudo usermod -d /home/odoo-semantic odoo-semantic
> ```

---

## 2. DB Tier — Neo4j + PostgreSQL

### 2.1 Cấu hình

**`.env`** — secrets cho Docker Compose (KHÔNG đọc bởi Python apps):

```bash
# Bắt buộc điền:
NEO4J_PASSWORD=<strong-password>
PG_PASSWORD=<strong-password>

# Giữ nguyên (hoặc bump version khi cần):
NEO4J_IMAGE=neo4j:5.26.25
```

**`odoo-semantic.conf`** — cấu hình Python app (đọc bởi indexer/manager/migrate/server):

```ini
[database]
neo4j_uri      = bolt://localhost:7687
neo4j_user     = neo4j
neo4j_password = <same-as-NEO4J_PASSWORD-in-.env>

pg_dsn = postgresql://odoo_semantic:<PG_PASSWORD>@localhost:5432/odoo_semantic
```

> **Quy tắc hai lớp config:**
> - `.env` → Docker Compose đọc khi `docker compose up`
> - `odoo-semantic.conf` → Python apps đọc (indexer, manager, migrate, mcp server)
> - Python apps **không** đọc `.env`. Secrets cần khai báo ở **cả hai** file.

### 2.2 Khởi động DB

```bash
docker compose up -d
docker compose ps   # cả hai service phải ở trạng thái healthy
```

Kiểm tra Neo4j:
```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1'
```

Kiểm tra PostgreSQL:
```bash
docker compose exec postgres pg_isready -U odoo_semantic
```

### 2.3 Ports — Same-server vs Split-tier

`docker-compose.yml` mặc định bind ports `127.0.0.1` (chỉ localhost):

```yaml
ports:
  - "127.0.0.1:7687:7687"   # Neo4j bolt — chỉ app cùng server truy cập được
  - "127.0.0.1:5432:5432"   # PostgreSQL
```

**Split-tier:** đổi thành `"0.0.0.0:7687:7687"` + chặn firewall — xem [§8](#8-split-tier-migration).

### 2.4 Backup thủ công (đến M5)

```bash
# Neo4j — dump database
docker compose exec neo4j \
    neo4j-admin database dump neo4j --to-path=/data/backups

# PostgreSQL
docker compose exec postgres \
    pg_dump -U odoo_semantic odoo_semantic \
    > ~/backups/odoo_semantic_$(date +%Y%m%d).sql
```

---

## 3. App Tier — Indexer + MCP Server

> **Quan trọng — chạy bằng user `odoo-semantic`:** Tất cả lệnh
> `make install`, `python -m src.*` từ đây phải chạy bằng user
> `odoo-semantic` để venv được tạo tại `/home/odoo-semantic/.venv/...`
> (consistent với `Makefile` + `odoo-semantic-mcp.service` ExecStart).
> Sandwich mọi lệnh trong:
> ```bash
> sudo -u odoo-semantic -H bash -c '<lệnh>'
> ```
> Cờ `-H` set `$HOME=/home/odoo-semantic` cho `make install`. Nếu bỏ
> `-H`, venv sẽ tạo ở `$HOME` của user invoke `sudo` → systemd service
> sẽ fail "No such file or directory" khi start.

### 3.1 Cài đặt

```bash
sudo git clone https://github.com/Viindoo/odoo-semantic-mcp /opt/odoo-semantic-mcp
sudo chown -R odoo-semantic:odoo-semantic /opt/odoo-semantic-mcp

# make install chạy bằng user odoo-semantic — tạo venv tại
# /home/odoo-semantic/.venv/odoo-semantic-mcp/ (NOT /root, NOT /home/<admin>)
sudo -u odoo-semantic -H bash -c '
    cd /opt/odoo-semantic-mcp && make install
'
# Verify:
sudo -u odoo-semantic ls /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python
```

### 3.2 Đặt config file

```bash
sudo mkdir -p /etc/odoo-semantic
sudo cp odoo-semantic.conf.example /etc/odoo-semantic/odoo-semantic.conf
sudo chmod 600 /etc/odoo-semantic/odoo-semantic.conf
sudo chown odoo-semantic:odoo-semantic /etc/odoo-semantic/odoo-semantic.conf
```

Điền passwords thật vào `odoo-semantic.conf`:

```ini
[database]
neo4j_uri      = bolt://localhost:7687
neo4j_user     = neo4j
neo4j_password = <NEO4J_PASSWORD>

pg_dsn = postgresql://odoo_semantic:<PG_PASSWORD>@localhost:5432/odoo_semantic

[server]
host = 127.0.0.1   # giữ nguyên — proxy tier sẽ handle external
port = 8002

[indexer]
repos_base_dir = /srv/odoo-repos
```

### 3.3 Bootstrap PostgreSQL schema

Chạy **một lần** sau khi DB tier (§2) healthy + venv (§3.1) đã tạo:

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
'
```

Output: `✓ Migrations applied to postgresql://...`

Lệnh idempotent — chạy lại không có hại.

### 3.4 Đăng ký repos + index lần đầu

> **Callout — M3 `find_examples` cần Ollama (embedder).**
> Lần index đầu dưới đây dùng `--no-embed` để bỏ qua embedder — đủ cho
> M1 (`resolve_model`/`field`/`method`), M2 (`resolve_view`), M4
> (`impact_analysis`). Khi muốn dùng `find_examples` (M3), setup Ollama
> theo [`docs/deploy/embedder-setup.md`](deploy/embedder-setup.md) rồi
> re-index **không** cờ `--no-embed`.

Admin clone repos vào server trước (chạy bằng `odoo-semantic` để
indexer đọc được):

```bash
sudo mkdir -p /srv/odoo-repos
sudo chown -R odoo-semantic:odoo-semantic /srv/odoo-repos

sudo -u odoo-semantic git clone --branch 17.0 \
    https://github.com/odoo/odoo /srv/odoo-repos/odoo_17.0
# ... clone thêm viindoo addons repos ...
```

Đăng ký + index (sandwich `sudo -u odoo-semantic -H`):

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    PY=/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python

    $PY -m src.manager add-profile viindoo_17 --version 17.0
    $PY -m src.manager add-repo \
        --profile viindoo_17 \
        --url github.com/odoo/odoo --branch 17.0 \
        --local-path /srv/odoo-repos/odoo_17.0
    $PY -m src.manager list   # verify

    # Lần đầu: index Neo4j graph only (đủ M1+M2+M4) — chưa cần Ollama
    $PY -m src.indexer --profile viindoo_17 --no-embed
    # Hoặc tất cả profiles:
    # $PY -m src.indexer --all --no-embed
'
```

Output: `Done: {'profiles_ok': 1, 'profiles_failed': [], 'modules': 412, 'views': 3801, 'qweb': 287}`

Khi đã setup embedder (xem `docs/deploy/embedder-setup.md`), re-index
**không** `--no-embed` để bổ sung embeddings cho M3:

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.indexer --profile viindoo_17
'
```

### 3.5 MCP server dạng systemd service

> User `odoo-semantic` đã tạo ở §1.1.

Copy systemd unit:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-mcp.service \
        /etc/systemd/system/

# Chỉnh sửa path nếu khác /opt/odoo-semantic-mcp:
sudo nano /etc/systemd/system/odoo-semantic-mcp.service

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-mcp
sudo systemctl status odoo-semantic-mcp
```

Xem logs:

```bash
sudo journalctl -u odoo-semantic-mcp -f
```

### 3.6 Re-index định kỳ (cron, đến M6)

```bash
sudo tee /etc/cron.d/odoo-semantic-reindex > /dev/null << 'EOF'
# Re-index toàn bộ profiles mỗi ngày lúc 3 giờ sáng
0 3 * * * odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer --all \
    >> /var/log/odoo-semantic-reindex.log 2>&1
EOF
```

### 3.7 tmux fallback (khi không có systemd)

```bash
sudo -u odoo-semantic -H tmux new -d -s odoo-semantic-mcp \
    'ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server'
sudo -u odoo-semantic tmux attach -t odoo-semantic-mcp   # để xem logs
```

---

## 4. Proxy Tier — Nginx hoặc Caddy

MCP server bind `127.0.0.1:8002` — **bắt buộc** có reverse proxy để external clients truy cập được.

### 4.1 Nginx

Copy và sửa config:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/nginx.conf.example \
        /etc/nginx/sites-available/odoo-semantic-mcp

# Thay semantic.example.com bằng domain thật
sudo nano /etc/nginx/sites-available/odoo-semantic-mcp

sudo ln -s /etc/nginx/sites-available/odoo-semantic-mcp \
           /etc/nginx/sites-enabled/
sudo nginx -t   # kiểm tra syntax
```

Lấy TLS cert:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d semantic.example.com
```

Reload:

```bash
sudo systemctl reload nginx
```

Cài đặt quan trọng trong `location /mcp` — bắt buộc cho SSE streaming:

```nginx
proxy_buffering    off;     # bắt buộc cho SSE — MCP dùng Server-Sent Events
proxy_read_timeout 3600s;   # MCP sessions có thể dài
```

Xem `docs/deploy/nginx.conf.example` để biết config đầy đủ, bao gồm các option auth.

### 4.2 Caddy (auto-TLS, đơn giản hơn)

```bash
sudo apt install caddy
```

Thêm vào `/etc/caddy/Caddyfile` (xem `docs/deploy/Caddyfile.example`):

```
semantic.example.com {
    reverse_proxy /mcp* 127.0.0.1:8002 {
        flush_interval -1
    }
}
```

```bash
sudo systemctl reload caddy
```

### 4.3 Auth (M2.5 — chưa có API key validation)

Chọn 1 option tạm thời:

| Option | Phù hợp cho | Config |
|--------|-------------|--------|
| IP allowlist | Internal team (static IP) | `allow 10.0.0.0/8; deny all;` trong nginx location block |
| HTTP Basic | Small team | `auth_basic` + `htpasswd` — xem comment trong `nginx.conf.example` |
| (Không có) | Dev/staging nội bộ | Chỉ khi server không public internet |

**Lưu ý:** `X-API-Key` trong cấu hình ví dụ Claude/VS Code là placeholder forward-compatible cho M5. Codebase **chưa validate** header này.

### 4.4 Verify proxy

```bash
curl -I https://semantic.example.com/mcp
# 200 hoặc 405 = MCP server đang chạy
# 502 = MCP server down, kiểm tra systemctl status odoo-semantic-mcp
```

---

## 5. E2E Smoke Test

Sau khi tất cả tiers đang chạy. Test cover full M1-M4 (M3 chỉ verify
được nếu đã setup embedder + re-index không `--no-embed`).

### 5.1 Quick verify từ DB tier (không cần MCP client)

Đếm nhanh để xác nhận indexer ghi đủ data:

```bash
# Neo4j — module + view + JS patch + OWL component count
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "
MATCH (m:Module {odoo_version:'17.0'}) WITH count(m) AS modules
MATCH (v:View   {odoo_version:'17.0'}) WITH modules, count(v) AS views
MATCH (jp:JSPatch {odoo_version:'17.0'}) WITH modules, views, count(jp) AS js_patches
RETURN modules, views, js_patches
"
# Expected (Odoo 17 base): modules ≥ 100, views ≥ 1000, js_patches ≥ 50

# PostgreSQL — registry status + embeddings count
docker compose exec postgres psql -U odoo_semantic -c "
SELECT name, status FROM repos;
SELECT count(*) AS embeddings FROM embeddings;
"
# Expected: status='indexed' cho mọi repo. embeddings = 0 nếu chạy
# --no-embed; > 0 sau khi re-index có embedder.
```

### 5.2 Verify qua MCP client (Claude Code)

Thêm vào `~/.claude/settings.json` trên laptop dev:

```json
{
  "mcpServers": {
    "odoo-semantic": {
      "url": "https://semantic.example.com/mcp"
    }
  }
}
```

Restart Claude Code, gọi 4 tool dưới đây. Mỗi tool cover 1 milestone:

Output các tool đều là text tree (`├─ ... └─ ...`) — đọc được trực tiếp.

| # | Tool call | Milestone | Expected (key snippets trong output) |
|---|-----------|-----------|--------------------------------------|
| 1 | `resolve_model("account.move", "17.0")` | M1 | Header `account.move (Odoo 17.0)`; section `Inheritance` ≥ 1 module; section `Fields` non-empty (vd `name`, `state`, `amount_total`). |
| 2 | `resolve_view("sale.view_order_form", "17.0")` | M2 | Header view xmlid; `View chain` ≥ 1 entry; `XPath modifications` list (có thể empty nếu chỉ view base). |
| 3 | `impact_analysis("field", "sale.order.amount_total", "17.0")` | M4 | Dòng `├─ Risk: <LOW\|MEDIUM\|HIGH>`; section `Views (N)` non-empty; `JS patches (N)`; `Dependent modules`. |
| 4 | `find_examples("compute tax based on partner country")` | M3 | List 5 results, mỗi entry có file path + score. **Skip nếu indexer chạy `--no-embed`** — tool sẽ báo "no embeddings indexed". |

Nếu tool trả rỗng:

```bash
# Kiểm tra repo status
docker compose exec postgres psql -U odoo_semantic \
    -c "SELECT name, status, last_indexed_at FROM repos;"
# status='indexed' → repo đã index
# status='error'   → xem indexer log

# Kiểm tra MCP server reach DB
sudo journalctl -u odoo-semantic-mcp -n 50 | grep -iE "neo4j|postgres|error"
```

---

## 6. Operational Runbook

### Vấn đề thường gặp

| Triệu chứng | Nguyên nhân phổ biến | Fix |
|-------------|---------------------|-----|
| 502 Bad Gateway | MCP server không chạy | `sudo systemctl start odoo-semantic-mcp` |
| "Không tìm thấy model" | Chưa index hoặc index lỗi | Kiểm tra `repos.status`, chạy lại indexer |
| Neo4j OOM | JVM heap thiếu | Tăng `NEO4J_server_memory_heap_max__size` trong `docker-compose.yml` |
| Index chậm | Nhiều module, network | Đây là expected — lần đầu ~10-30 phút cho 400+ modules |
| `✗ Cannot connect to PostgreSQL` | PG chưa healthy / sai DSN | `docker compose ps`, kiểm tra `pg_dsn` trong conf |

### Log locations

| Thành phần | Lệnh xem log |
|------------|-------------|
| MCP server | `sudo journalctl -u odoo-semantic-mcp -f` |
| Indexer (cron) | `tail -f /var/log/odoo-semantic-reindex.log` |
| Neo4j | `docker compose logs -f neo4j` |
| PostgreSQL | `docker compose logs -f postgres` |
| Nginx | `/var/log/nginx/error.log` |

### Restart / Reload

```bash
# MCP server (không ảnh hưởng DB)
sudo systemctl restart odoo-semantic-mcp

# Sau khi index xong
sudo systemctl status odoo-semantic-mcp   # verify vẫn running

# DB restart (hiếm khi cần)
docker compose restart neo4j
docker compose restart postgres
```

---

## 7. Security Checklist

Trước khi expose public internet:

- [ ] `.env` và `odoo-semantic.conf` có quyền `600`, owner `odoo-semantic`
- [ ] `NEO4J_PASSWORD` và `PG_PASSWORD` không phải default `password`
- [ ] Neo4j và PG ports bind `127.0.0.1` (kiểm tra `docker compose ps` — cột Ports)
- [ ] MCP server bind `127.0.0.1` (kiểm tra `odoo-semantic.conf [server] host`)
- [ ] TLS cert valid + auto-renewing (certbot timer: `systemctl status certbot.timer` hoặc Caddy auto)
- [ ] Auth option đã chọn (IP allowlist / Basic Auth)
- [ ] Service user `odoo-semantic` là non-login (`shell=/usr/sbin/nologin`)
- [ ] Backup đã được test (restore thử ít nhất 1 lần)

---

## 8. Split-Tier Migration

Khi cần tách DB ra VM riêng (≥80 users, hoặc HA):

1. Move `docker-compose.yml` và `.env` sang DB VM.
2. Đổi ports binding từ `127.0.0.1:7687:7687` → `0.0.0.0:7687:7687`.
3. Cấu hình firewall DB VM: chỉ cho phép app VM IP kết nối port 7687 và 5432.
4. Set `NEO4J_ADVERTISED_HOST=<DB-VM-public-IP>` trong `.env` (bắt buộc — bolt client dùng advertised address để redirect).
5. Trên App VM: cập nhật `odoo-semantic.conf`:
   ```ini
   [database]
   neo4j_uri = bolt://<DB-VM-IP>:7687
   pg_dsn    = postgresql://odoo_semantic:<pass>@<DB-VM-IP>:5432/odoo_semantic
   ```
6. `sudo systemctl restart odoo-semantic-mcp`
7. Smoke test (§5).

---

## 9. Embedder Setup (M3 Semantic Wow)

`find_examples` tool dùng **Qwen3-Embedding-4B Q5_K_M** qua Ollama. Cần setup một lần trước khi chạy indexer với embeddings.

### 9.1 Cài Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
```

### 9.2 Tải model GGUF + tạo Modelfile

Default `ollama pull qwen3-embedding:4b` ship Q4_K_M. Dùng Q5_K_M để có chất lượng cao hơn:

```bash
# Download Q5_K_M từ HuggingFace (cần ~3.2 GB)
mkdir -p ~/.ollama/models/gguf
wget -O ~/.ollama/models/gguf/qwen3-embedding-4b-q5km.gguf \
  "https://huggingface.co/Qwen/Qwen3-Embedding-GGUF/resolve/main/Qwen3-Embedding-4B-Q5_K_M.gguf"

# Tạo Modelfile
cat > /tmp/Modelfile-qwen3-embed << 'EOF'
FROM /root/.ollama/models/gguf/qwen3-embedding-4b-q5km.gguf
EOF

# Register với Ollama
ollama create qwen3-embedding-q5km -f /tmp/Modelfile-qwen3-embed

# Kiểm tra
ollama run qwen3-embedding-q5km "test" || echo "embed OK"
```

### 9.3 Cấu hình server

Thêm vào `odoo-semantic.conf`:

```ini
[embedder]
url = http://localhost:11434
model = qwen3-embedding-q5km
dim = 1024
```

Hoặc dùng env vars: `EMBEDDER_URL`, `EMBEDDER_MODEL`, `EMBEDDER_DIM`.

### 9.4 Bootstrap pgvector extension

Chạy một lần với superuser PostgreSQL:

```bash
# Khi dùng docker-compose (init script tự động)
docker compose down && docker compose up -d postgres

# Hoặc thủ công:
PGPASSWORD=<superuser-pass> psql -h localhost -U postgres -d odoo_semantic \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Sau đó run migrations:

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
```

### 9.5 Run indexer với embeddings

Mặc định indexer sẽ embed (đọc `[embedder]` từ `odoo-semantic.conf`). Dùng `--no-embed` nếu chỉ muốn index Neo4j graph mà không ghi embeddings:

```bash
# Index đầy đủ: Neo4j graph + pgvector embeddings (cần Ollama đang chạy)
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --profile viindoo_17

# Index chỉ Neo4j graph (không cần Ollama)
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --profile viindoo_17 --no-embed

# Index tất cả profiles
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --all
```

Indexer sẽ gọi Ollama để tạo embeddings cho mỗi module. ~400 modules × ~500 chunks × 1024 dim ≈ 20 GB disk. Thời gian: ~30-60 phút lần đầu (incremental sau đó <5 phút).

> **Fresh install PostgreSQL:** volume `pg_data` chỉ được init khi tạo mới. Nếu volume đã tồn tại và thiếu pgvector, chạy `docker compose down -v && docker compose up -d` để reinit.

### 9.6 License note

Qwen3-Embedding Apache 2.0. MS MARCO training data có issue đang pending (QwenLM/Qwen3-Embedding#166). **Internal tooling: OK. External SaaS**: cần legal review trước khi ship.
