# Production Deploy — Odoo Semantic MCP

Hướng dẫn này dành cho **admin** deploy server.
- Developer xem [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- End-user kết nối AI tool xem [`docs/client-setup.md`](client-setup.md).

> **System Requirements:** xem [README §System Requirements](../README.md#system-requirements-server) cho bảng CPU/RAM/SSD đầy đủ. Tóm tắt: all-in-one ≤30 users = 2 vCPU/8 GB/50 GB; full stack ≤80 users = 4 vCPU/16 GB/150 GB.

---

## 0. Topology

Doc cover **2 topology** — chọn theo môi trường:

### Topology A — All-in-one (dev / E2E test / ≤30 users)

Tất cả service trên 1 host. Đơn giản, đủ cho dev test E2E hoặc team
nhỏ. Đây là default doc khi không nói rõ split.

```
Người dùng (AI tool)
        │ HTTPS :443
        ▼
  ┌────────────────────────────────────────────┐
  │  HOST DUY NHẤT                             │
  │                                            │
  │  Nginx/Caddy  (reverse proxy + TLS)        │
  │      │ 127.0.0.1:8002                      │
  │      ▼                                     │
  │  MCP Server   (systemd, user odoo-semantic)│
  │      │            │              │          │
  │  bolt│       psycopg2│        HTTP│         │
  │  7687│         5432  │       11434│         │
  │      ▼            ▼              ▼          │
  │  Neo4j ─── Postgres ─── Ollama (M3 only)   │
  │  (docker)  (docker)     (systemd)          │
  └────────────────────────────────────────────┘
```

DB ports + Ollama bind `127.0.0.1` — không expose ra ngoài.

### Topology B — Split-tier (production / ≥80 users / HA / shared embedder)

4 tier riêng host. Đặc biệt phù hợp khi đã có **Ollama instance dùng
chung** cho nhiều dự án — không cần cài lại trên server MCP.

```
                  Người dùng (AI tool)
                          │ HTTPS
                          ▼
                  ┌────────────────┐
                  │  Proxy tier    │  Nginx/Caddy + TLS
                  └────────┬───────┘
                           │ HTTP (private)
                           ▼
                  ┌────────────────┐
                  │  App tier      │  MCP server + indexer venv
                  │  (odoo-semantic)│
                  └──┬──────┬───┬──┘
              bolt  │  pg  │   │ HTTP 11434
              7687  │ 5432 │   │
                    ▼      ▼   ▼
              ┌─────────────┐  ┌──────────────────┐
              │ DB tier     │  │ Embedder tier    │
              │ Neo4j +     │  │ Ollama (có thể   │
              │ Postgres    │  │ shared multi-app)│
              └─────────────┘  └──────────────────┘
```

Mỗi tier bind `0.0.0.0` + firewall whitelist IP App tier. Ollama không
có auth built-in → SSH tunnel hoặc TLS reverse proxy nếu qua Internet
(xem `docs/deploy/embedder-setup.md` §4).

Migration A → B: xem [§8 Split-Tier](#8-split-tier-migration).

### Tool dependency theo milestone

Không phải tool nào cũng cần đầy đủ stack — admin có thể defer setup
embedder cho đến khi cần M3:

| Tool | M1-M2 graph | M3 Semantic (Ollama) | M4 Impact | M4.5 Spec | M4.6 Pattern |
|------|:-:|:-:|:-:|:-:|:-:|
| `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` | ✓ | — | — | — | — |
| `find_examples` | — | ✓ | — | — | — |
| `impact_analysis` | — | — | ✓ | — | — |
| `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help` | — | — | — | ✓ | — |
| `suggest_pattern`, `check_module_exists`, `find_override_point` | — | — | — | — | ✓ |

Mỗi cột tương ứng với 1 lệnh setup ở §3.4 dưới đây:

- **M1-M2 + M4** ← `index-repo --no-embed` (Neo4j + Postgres registry)
- **M3** ← `index-repo` (không `--no-embed`) — cần Ollama embedder
- **M4.5** ← `index-core --version <X.0> --source <odoo_X.0_clone>` (per version)
- **M4.6** ← `python -m src.indexer.seed_patterns` (one-shot, idempotent)

Test E2E M1+M2+M4 chỉ cần Neo4j + PostgreSQL (registry). M3 + M4.6
embed thêm Ollama — defer được, xem `docs/deploy/embedder-setup.md`.

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

Lệnh idempotent — chạy lại không có hại. Tạo các tables:
`profiles`, `repos`, `embeddings` (cần `pgvector`), `api_keys`, `ssh_key_pairs`,
`usage_log`, `pattern_feedback`, **`indexer_jobs`** (M5.5 — track lifecycle của
indexer subprocess; populated bởi `index-repo --job-id N` + Web UI status badge).

> **M6 Wave 1 — fail-fast version checks:** `run_migrations()` đầu function
> verify `server_version_num >= 160000` (PostgreSQL 16+). Sau khi `CREATE EXTENSION
> vector` thành công verify `extversion >= 0.8`. Migrations abort với `RuntimeError`
> nếu một trong hai dưới min — nâng cấp `PG_IMAGE` trong `.env.example` rồi re-run.
> MCP server startup (`src/mcp/server.py _get_driver()`) tương tự verify Neo4j ≥ 5.x
> qua `CALL dbms.components()`.

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
    $PY -m src.indexer index-repo --profile viindoo_17 --no-embed
    # Hoặc tất cả profiles:
    # $PY -m src.indexer index-repo --all --no-embed
'
```

Output: `Done: {'profiles_ok': 1, 'profiles_failed': [], 'modules': 412, 'views': 3801, 'qweb': 287}`

Khi đã setup embedder (xem `docs/deploy/embedder-setup.md`), re-index
**không** `--no-embed` để bổ sung embeddings cho M3:

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.indexer index-repo --profile viindoo_17
'
```

> **Thời lượng thực tế khi index có embed:** Embedder là chokepoint —
> 605 modules / ~46k embedding chunks ≈ **3–6 giờ** qua remote Ollama
> proxy (≈22s/100 texts trên qwen3-embedding-q5km, batch 50). Local
> Ollama nhanh hơn 1.5–2x. SSH có thể timeout giữa chừng → detach
> bằng `tmux` (xem §3.7) hoặc:
> ```bash
> sudo -u odoo-semantic -H setsid nohup bash -c '
>     export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
>     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
>         -m src.indexer index-repo --profile viindoo_17 --verbose
> ' > /var/log/odoo-semantic-index.log 2>&1 &
> # Theo dõi tiến độ:
> sudo tail -f /var/log/odoo-semantic-index.log
> ```
> Index-repo là idempotent (cập nhật theo content hash) — chạy lại an
> toàn nếu bị ngắt giữa chừng.

#### 3.4.1 Index Odoo core specs (M4.5 — `lookup_core_api` & friends)

Cần cho 5 tool M4.5: `lookup_core_api`, `api_version_diff`,
`find_deprecated_usage`, `lint_check`, `cli_help`. Chạy 1 lệnh per
version (mỗi version index từ source clone Odoo upstream). Idempotent.

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    PY=/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python

    $PY -m src.indexer index-core --source /srv/odoo-repos/odoo_17.0 --version 17.0
    # Thêm version để api_version_diff so sánh nhiều phiên bản:
    # $PY -m src.indexer index-core --source /srv/odoo-repos/odoo_18.0 --version 18.0
    # $PY -m src.indexer index-core --source /srv/odoo-repos/odoo_16.0 --version 16.0
    # v8/v9 hỗ trợ qua __openerp__.py finder (M4.5 Phase 0):
    # $PY -m src.indexer index-core --source /srv/odoo-repos/odoo_8.0  --version 8.0
'
```

Output mong đợi: `Done: 502 CoreSymbol, 16 LintRule, 12 CLICommand, 80 CLIFlag` (per version, ±10%).
Mất 30–60s/version.

#### 3.4.2 Seed pattern catalogue (M4.6 — `suggest_pattern` & friends)

One-shot, idempotent. Cần cho `suggest_pattern`, `check_module_exists`,
`find_override_point`.

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.indexer.seed_patterns
    # --no-embed nếu chưa setup Ollama (Neo4j nodes vẫn có,
    # chỉ thiếu semantic ranking trong suggest_pattern):
    # ... -m src.indexer.seed_patterns --no-embed
'
```

Output: `INFO seed_patterns: Neo4j: wrote 54 PatternExample nodes` +
`INFO seed_patterns: pgvector: wrote N embedding chunks`.

### 3.5 systemd services (MCP + Web UI)

> User `odoo-semantic` đã tạo ở §1.1.

Repo ship 2 unit file ở `docs/deploy/`:

| File | Service | Bind | Cần |
|------|---------|------|-----|
| `odoo-semantic-mcp.service` | MCP server (port 8002) | `127.0.0.1` qua proxy tier | INI config |
| `odoo-semantic-webui.service` | Web UI admin (port 8003) | `127.0.0.1` LAN-only | INI config + `webui.env` (FERNET_KEY) |

Cài MCP unit:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-mcp.service \
        /etc/systemd/system/

# Chỉnh sửa path nếu khác /opt/odoo-semantic-mcp:
sudo nano /etc/systemd/system/odoo-semantic-mcp.service

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-mcp
sudo systemctl status odoo-semantic-mcp
```

Cài Web UI unit (cần FERNET_KEY — xem §12 để generate):

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-webui.service \
        /etc/systemd/system/

# Tạo file secrets riêng cho Web UI — KHÔNG commit, mode 600:
sudo install -o odoo-semantic -g odoo-semantic -m 600 /dev/null \
    /etc/odoo-semantic/webui.env
sudo tee /etc/odoo-semantic/webui.env > /dev/null <<EOF
FERNET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
EOF

sudo systemctl enable --now odoo-semantic-webui
sudo systemctl status odoo-semantic-webui
```

⚠️ **Backup `webui.env` an toàn (vd password manager).** Nếu mất
FERNET_KEY → mọi SSH private key đã lưu trong DB không giải mã được.

Xem logs:

```bash
sudo journalctl -u odoo-semantic-mcp -f
sudo journalctl -u odoo-semantic-webui -f
```

### 3.6 Re-index định kỳ (cron, M6 Wave 2 incremental ready)

```bash
sudo tee /etc/cron.d/odoo-semantic-reindex > /dev/null << 'EOF'
# Re-index toàn bộ profiles mỗi giờ — M6 Wave 2 incremental skips unchanged repos.
0 * * * * odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all \
    --profile-workers 2 \
    >> /var/log/odoo-semantic-reindex.log 2>&1

# Monthly --full reindex để clean stale Module nodes từ rename/move (per ADR-0007).
0 4 1 * * odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all --full \
    >> /var/log/odoo-semantic-reindex.log 2>&1
EOF
```

> **M6 Wave 2 — incremental indexer:** `pipeline._index_repo` so sánh git HEAD với
> `repos.head_sha` stored. Repo unchanged → zero-cost skip. Otherwise `git diff` để
> filter scan results to changed modules only. `--profile-workers N` để index multi-version
> đồng thời (per-profile lock đảm bảo safe). `--full` flag bypass skip cho periodic cleanup.
> Auto-reseed pattern catalogue cũng wire vào pipeline (sha256 sentinel — cheap khi unchanged).
> See `docs/adr/0007-incremental-indexer.md` cho design decisions.

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

### 4.3 Auth (M5 — X-API-Key required)

Từ M5, mọi request tới `/mcp` **phải** có header `X-API-Key` hợp lệ.
Request thiếu key hoặc key không active → `401 Unauthorized`. Không có bypass.

**Tạo API key (admin):**

```bash
# Via CLI:
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key <name>
# → osm_xxxx... (raw key — hiển thị một lần duy nhất, lưu ngay)

# Via Web UI (http://127.0.0.1:8003/api-keys):
# Dashboard → API Keys → Create API Key → điền tên → Copy raw key ngay
```

**Phân phát key cho user:**
- Gửi raw key qua kênh bảo mật (Bitwarden, 1Password — không qua email plain text) cùng URL `https://<your-domain>:9999/install/` — user dán key vào trang đó để nhận snippet đúng cho từng AI tool (không cần copy-paste từ docs)
- Mỗi user/team nên có key riêng để revoke độc lập nếu cần
- Deactivate key: Web UI → Deactivate, hoặc CLI: `UPDATE api_keys SET active=false WHERE name='<name>';`

**Proxy phải forward header `X-API-Key`** (nginx mặc định không strip header — không cần config thêm nếu dùng `proxy_pass`). Caddy forward tất cả headers mặc định.

**/health bypass auth:** `GET /health` không yêu cầu API key — dùng cho load balancer health check:

```bash
curl http://127.0.0.1:8002/health
# → {"neo4j": "ok", "postgres": "ok"}
```

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
      "url": "https://semantic.example.com/mcp",
      "headers": { "X-API-Key": "<raw-key-từ-create-api-key>" }
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
| Index chậm (no embed) | Nhiều module | Expected — `index-repo --no-embed` ~10-30 phút cho 400-600 modules (CPU-bound trên file parsing) |
| Index rất chậm (có embed) | Embedder là chokepoint | Expected — `index-repo` (full) ~3-6h qua remote Ollama, ~1.5-3h local. Detach bằng `setsid nohup` hoặc tmux (xem §3.4 callout) |
| Indexer fail giữa chừng | SSH timeout / OOM Ollama | Idempotent — chạy lại từ đầu an toàn (content hash dedup). Nếu OOM Ollama: giảm batch trong `src/indexer/embedder.py` (default 50) hoặc dùng model nhỏ hơn |
| `lookup_core_api` / `cli_help` rỗng | Chưa chạy `index-core` | Chạy §3.4.1 cho version cần dùng |
| `suggest_pattern` rỗng | Chưa chạy `seed_patterns` | Chạy §3.4.2 (idempotent) |
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

Khi chuyển từ Topology A (all-in-one) sang Topology B (production /
HA / shared embedder).

### 8.1 Tách DB tier (Neo4j + Postgres) ra VM riêng

1. Move `docker-compose.yml` và `.env` sang DB VM.
2. Đổi ports binding từ `127.0.0.1:7687:7687` → `0.0.0.0:7687:7687`
   (cả `5432:5432`).
3. Firewall DB VM: chỉ cho phép App VM IP kết nối port 7687 và 5432.
4. Set `NEO4J_ADVERTISED_HOST=<DB-VM-public-IP>` trong `.env` (bắt
   buộc — bolt client dùng advertised address để redirect).
5. Trên App VM: cập nhật `odoo-semantic.conf`:
   ```ini
   [database]
   neo4j_uri = bolt://<DB-VM-IP>:7687
   pg_dsn    = postgresql://odoo_semantic:<pass>@<DB-VM-IP>:5432/odoo_semantic
   ```
6. `sudo systemctl restart odoo-semantic-mcp`
7. Smoke test §5.

### 8.2 Tách Embedder tier (Ollama) ra VM riêng (hoặc dùng instance shared)

Ollama có thể chạy trên VM riêng — đặc biệt hữu ích khi đã có instance
phục vụ nhiều dự án (vd 1 GPU server share giữa MCP + chat coder +
auto-complete). Setup chi tiết: xem
[`docs/deploy/embedder-setup.md`](deploy/embedder-setup.md).

Tóm tắt 4 bước:

1. **Trên Embedder VM**: setup Ollama + `OLLAMA_HOST=0.0.0.0:11434`
   (xem embedder-setup.md §2 + §4) + add model `qwen3-embedding-q5km`
   (xem §3).
2. **Firewall**: chỉ allow App VM IP truy cập port `11434` (Ollama
   không có auth built-in — KHÔNG expose Internet trực tiếp).
3. **Trên App VM**: cập nhật `odoo-semantic.conf`:
   ```ini
   [embedder]
   url   = http://<embedder-vm-ip>:11434
   model = qwen3-embedding-q5km
   dim   = 1024
   ```
4. Re-index không `--no-embed` (xem §3.4) → smoke test
   `find_examples` (xem §5.2 row #4).

---

## 9. Embedder Setup (M3 Semantic Wow)

Backend embedder cho `find_examples` (M3) tách thành file riêng vì
support 3 topology (local / remote dedicated / remote shared) và bước
add-model dùng được cho admin đã có Ollama instance từ dự án khác:

→ **[`docs/deploy/embedder-setup.md`](deploy/embedder-setup.md)**

Nếu test E2E M1+M2+M4 (không cần `find_examples`), skip section này.
Indexer chạy với `--no-embed` (xem §3.4) là đủ.

---

## 10. API Key Auth (M5)

MCP server (port 8002) yêu cầu `X-API-Key` header với mọi request trừ `GET /health`.

### Tạo API key đầu tiên

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key admin
# → Prints: osm_xxxxxxxxxxxx...  (shown once — save this)
```

Key được hash SHA-256 trong DB. Raw key không được lưu lại — nếu mất phải tạo key mới.

### Quản lý key qua Web UI

Hoặc tạo/deactivate key tại http://127.0.0.1:8003/api-keys (xem §11).

### LRU Cache

Server cache kết quả verify trong 5 phút để giảm DB load. Khi deactivate key, cache tự expire sau 5 phút.

### Truyền key cho AI tool

**Claude Code** — thêm vào `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "odoo-semantic": {
      "url": "https://semantic.viindoo.com/mcp",
      "headers": { "X-API-Key": "osm_xxxx..." }
    }
  }
}
```

---

## 11. Web UI Admin (M5)

Web UI quản lý profiles, repos, API keys, SSH keys.

**Port**: 8003  
**Bind**: `127.0.0.1` only (không expose ra internet — không có auth!)  
**Access qua**: SSH tunnel hoặc Nginx proxy với IP allowlist

### Khởi động

**Production (recommended)** — systemd unit (đã document ở §3.5):

```bash
sudo systemctl enable --now odoo-semantic-webui
sudo systemctl status odoo-semantic-webui
```

Service file ship sẵn ở `docs/deploy/odoo-semantic-webui.service` —
có `EnvironmentFile=/etc/odoo-semantic/webui.env` để load FERNET_KEY
(cần cho SSH key encrypt/decrypt). Setup webui.env xem §3.5.

**Foreground / dev** — chạy trực tiếp (đảm bảo `FERNET_KEY` trong env):

```bash
ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
FERNET_KEY=<key> \
~/.venv/odoo-semantic-mcp/bin/python -m src.web_ui
# → http://127.0.0.1:8003/
```

### Nginx proxy (nếu cần truy cập từ xa)

Thêm vào nginx config (chỉ dùng với IP allowlist hoặc VPN):
```nginx
location /admin/ {
    allow 10.0.0.0/8;   # internal only
    deny all;
    proxy_pass http://127.0.0.1:8003/;
}
```

⚠️ **KHÔNG expose Web UI trực tiếp ra internet** — không có authentication.

### Indexer Job Status (M5.5 F)

Khi admin click "Index" trên `/repos`, Web UI:

1. Tạo row trong `indexer_jobs` (`status='queued'`, `profile_name`, `created_at`).
2. Spawn `python -m src.indexer index-repo --profile X --job-id N` (fire-and-forget).
3. Subprocess update `status='running'` + `pid` + `started_at` ngay khi bắt đầu.
4. Khi xong: `status='done'` + `finished_at`. Nếu fail: `status='error'` + `error_msg` (truncated 1000 chars) + `finished_at`.

Status badge trên `/repos` page poll `GET /repos/jobs/{job_id}/status` mỗi 5s khi
status ∈ `{queued, running}`; tự dừng khi reach `{done, error}`. Vanilla JS, không
dependency frontend.

**Endpoint JSON shape:**
```json
{
  "id": 42, "profile_name": "viindoo_17", "status": "running",
  "pid": 12345, "started_at": "2026-05-10 10:00:00+00:00",
  "finished_at": null, "error_msg": null,
  "created_at": "2026-05-10 09:59:58+00:00"
}
```

**Operational queries** (psql):

```sql
-- Jobs đang chạy:
SELECT id, profile_name, pid, started_at FROM indexer_jobs WHERE status='running';

-- 10 job gần nhất:
SELECT id, profile_name, status, started_at, finished_at, error_msg
FROM indexer_jobs ORDER BY created_at DESC LIMIT 10;

-- Cleanup jobs cũ (nếu cần):
DELETE FROM indexer_jobs WHERE created_at < now() - INTERVAL '30 days';
```

`indexer_jobs` không tự cleanup — nếu trở thành lớn, schedule cron `DELETE` hoặc
add migration trong M6.

---

## 12. SSH Keys (M5)

Web UI có thể generate Ed25519 keypair để clone private Odoo repos.

### Yêu cầu: FERNET_KEY

Private key được encrypt bằng Fernet symmetric encryption. `FERNET_KEY`
chỉ đọc từ env var (không từ INI). Production deploy đặt trong
`/etc/odoo-semantic/webui.env` (đã document ở §3.5):

```bash
# Generate key (chạy một lần, lưu an toàn):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Đặt vào webui.env (loaded bởi systemd unit):
echo "FERNET_KEY=<output_above>" | sudo tee /etc/odoo-semantic/webui.env
sudo chmod 600 /etc/odoo-semantic/webui.env
sudo chown odoo-semantic:odoo-semantic /etc/odoo-semantic/webui.env
sudo systemctl restart odoo-semantic-webui
```

Dev mode (chạy `python -m src.web_ui` trực tiếp): export `FERNET_KEY` trong shell hoặc thêm vào `.env` rồi `set -a; source .env; set +a`.

⚠️ **Nếu mất FERNET_KEY**: mọi SSH private key đã lưu sẽ không giải
mã được. Backup `webui.env` an toàn (vd password manager). Indexer/MCP
server không cần FERNET_KEY runtime — chỉ Web UI và CLI
`rotate-fernet` cần.

### Generate keypair

1. Truy cập http://127.0.0.1:8003/ssh-keys
2. Nhập tên → Generate
3. Copy public key → thêm vào GitHub/GitLab Deploy Keys
4. Private key được lưu encrypted trong DB

---

## 13. Backup (M5.5)

### Backup PostgreSQL — CLI (M5.5+)

```bash
python -m src.cli backup --output backup_$(date +%Y%m%d).sql
```

Hoặc chạy `pg_dump` trực tiếp:

```bash
pg_dump -h localhost -U odoo_semantic odoo_semantic > backup_$(date +%Y%m%d).sql
```

### Backup Neo4j

Dùng Neo4j Browser hoặc cypher-shell:
```bash
neo4j-admin database dump neo4j --to-path=/backups/
```

Hoặc đơn giản hơn: copy thư mục Neo4j data khi service stopped.

### Restore

```bash
psql -h localhost -U odoo_semantic odoo_semantic < backup_2026XXXX.sql
```

### FERNET key rotation (M5.5+)

Nếu cần đổi FERNET_KEY (vd: key bị lộ):

```bash
python -m src.cli rotate-fernet \
  --old-key <FERNET_KEY_cũ> \
  --new-key <FERNET_KEY_mới>
```

Cập nhật `FERNET_KEY` trong `.env` sau khi rotate xong.

⚠️ **M6 sẽ thêm**: automated backup script + S3 upload.
