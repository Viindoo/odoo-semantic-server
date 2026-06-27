# Production Deploy — Odoo Semantic MCP

Hướng dẫn này dành cho **admin** deploy server.
- Developer xem [`CONTRIBUTING.md`](../CONTRIBUTING.md).
- End-user kết nối AI tool xem [client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/setup.md).

> **System Requirements:** xem [README §System Requirements](../README.md#system-requirements-server) cho bảng CPU/RAM/SSD đầy đủ. Tóm tắt: all-in-one ≤30 users = 2 vCPU/8 GB/50 GB; full stack ≤80 users = 4 vCPU/16 GB/150 GB.

---

## 0. Topology

Doc cover **2 topology** — chọn theo môi trường:

### Topology A — All-in-one (dev / E2E test / ≤30 users)

Tất cả service trên 1 host. Đơn giản, đủ cho dev test E2E hoặc team
nhỏ. Đây là default doc khi không nói rõ split.

```
Người dùng (Browser / AI tool)
        │ HTTPS :443
        ▼
  ┌──────────────────────────────────────────────────┐
  │  HOST DUY NHẤT                                   │
  │                                                  │
  │  Nginx/Caddy  (reverse proxy + TLS)              │
  │      │ /          → 127.0.0.1:4321 (Astro SSR)  │
  │      │ /admin/*   → 127.0.0.1:4321 (Astro SSR)  │
  │      │ /api/*     → 127.0.0.1:8003 (FastAPI)    │
  │      │ /mcp       → 127.0.0.1:8002 (MCP)        │
  │      ▼                                           │
  │  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
  │  │Astro SSR │  │FastAPI   │  │MCP Server│       │
  │  │:4321     │  │:8003     │  │:8002     │       │
  │  │(Node.js) │  │(Python)  │  │(Python)  │       │
  │  └──────────┘  └──────────┘  └──────────┘       │
  │        │            │              │             │
  │        └────────────┴──────────────┘             │
  │              bolt:7687  pg:5432  http:11434       │
  │                    ▼       ▼         ▼            │
  │  Neo4j ───── Postgres ─── Ollama (M3 only)       │
  │  (docker)    (docker)     (systemd)               │
  └──────────────────────────────────────────────────┘
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

## 0.5 System Requirements

### Minimum — ~30 người dùng, M1–M2

```
2 vCPU / 8 GB RAM / 50 GB SSD
```

| Thành phần | RAM |
|------------|-----|
| Neo4j 5 (JVM heap) | 4 GB |
| MCP Server (Python) | 300 MB |
| OS + buffer | ~3.7 GB |

**Đáp ứng được:**
- 30 người dùng đồng thời (20% dev, 80% business)
- ~2.000 MCP queries/ngày, peak ~10 req/phút
- Odoo ecosystem ~400 modules: ~50.000 nodes, ~100.000 edges trong Neo4j
- Tất cả queries có composite index → latency 2–10ms/request

**Chưa đáp ứng:** M3 Semantic Wow (pgvector embeddings cần thêm RAM cho PostgreSQL).

---

### Recommended — ~30 người dùng, M1–M5 đầy đủ

```
4 vCPU / 16 GB RAM / 150 GB SSD
```

| Thành phần | RAM |
|------------|-----|
| Neo4j 5 (JVM heap) | 4 GB |
| PostgreSQL 16 + pgvector | 4 GB |
| MCP Server + Web UI (Python) | 1 GB |
| OS + buffer + peak headroom | ~7 GB |

**Đáp ứng được:**
- Toàn bộ M1–M5: graph queries + semantic search (pgvector) + Web UI admin + CLI indexer
- Mở rộng lên ~80 người dùng mà không cần thay đổi cấu hình
- Re-index ~400 modules trong <60 giây (incremental M6)
- Storage: Neo4j data (~5 GB) + PostgreSQL embeddings (~20 GB) + Odoo repos (~10 GB) + headroom

**Tách tier khi nào:** Khi đội >100 người hoặc cần HA — tách Neo4j + PostgreSQL ra VM riêng, giữ App tier nhẹ (2 vCPU / 4 GB).

---

## 1. Prerequisites

| Thứ | Phiên bản | Dùng cho |
|-----|-----------|---------|
| Ubuntu 24.04 LTS | Noble | OS khuyến nghị |
| Docker Engine | 24+ | DB tier |
| Python | 3.12 | App tier |
| uv | 0.4+ | Package manager |
| **Node.js** | **22 LTS+** | **Astro SSR service (M8)** — Node 24 khuyến nghị từ tháng 6/2026 |
| **pnpm** | bất kỳ | **Astro build tool** — `npm i -g pnpm` hoặc `corepack enable pnpm` |
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

**`.env`** — secrets cho Docker Compose (cũng được Python apps auto-load với `override=False`, xem ADR-0031):

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
> - Python apps **cũng auto-load `.env`** qua `python-dotenv` với `override=False`
>   (ADR-0031, issue #141) — env vars do systemd `EnvironmentFile=` hoặc shell
>   inject vẫn thắng, `.env` chỉ điền slot còn trống. Production thường khai
>   báo secrets trong `odoo-semantic.conf` + systemd env, không phụ thuộc `.env`.

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

### 2.4 Backup thủ công

```bash
# Tạo thư mục backup local nếu chưa có:
mkdir -p ~/backups

# Neo4j — dump database vào container, rồi copy ra host:
docker compose exec neo4j sh -c 'mkdir -p /data/backups && neo4j-admin database dump neo4j --to-path=/data/backups'
docker cp odoo-semantic-mcp-neo4j-1:/data/backups/neo4j.dump ~/backups/neo4j-$(date +%F).dump

# PostgreSQL — dump toàn bộ DB ra file SQL:
docker compose exec postgres \
    pg_dump -U odoo_semantic odoo_semantic \
    > ~/backups/odoo_semantic_$(date +%Y%m%d).sql
```

**Restore Neo4j** (khi cần phục hồi từ dump):

```bash
# Copy dump vào container trước:
docker cp ~/backups/neo4j-<DATE>.dump odoo-semantic-mcp-neo4j-1:/data/backups/
# Load (service phải stopped hoặc database offline):
docker compose exec neo4j neo4j-admin database load neo4j --from-path=/data/backups --overwrite-destination=true
```

> Xem `docs/deploy/disaster-recovery.md` để biết RTO estimate + restore order đầy đủ.

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
>
> **App-dir + config-dir choice (ADR-0027 §2):** canonical layout dùng
> `/home/odoo-semantic/odoo-semantic-mcp/` cho app + `/home/odoo-semantic/etc/`
> cho config (consistent với systemd templates ở `docs/deploy/*.service`).
> Snippets bên dưới dùng layout legacy `/opt/odoo-semantic-mcp` +
> `/etc/odoo-semantic/` (vẫn valid — chỉ cần drop-in overrides cho systemd
> units, xem [`docs/deploy/install-runbook.md`](deploy/install-runbook.md) +
> [`docs/deploy/overrides/`](deploy/overrides/)). Khi chọn canonical
> layout, substitute `/opt/odoo-semantic-mcp` → `/home/odoo-semantic/odoo-semantic-mcp`
> và `/etc/odoo-semantic/` → `/home/odoo-semantic/etc/` trong mọi lệnh sau đây.

### 3.1 Cài đặt

> **Note:** This is a private Viindoo repository — cloning requires org membership or a granted deploy key.

```bash
sudo git clone https://github.com/Viindoo/odoo-semantic-server /opt/odoo-semantic-mcp
sudo chown -R odoo-semantic:odoo-semantic /opt/odoo-semantic-mcp

# make install chạy bằng user odoo-semantic — tạo venv tại
# /home/odoo-semantic/.venv/odoo-semantic-mcp/ (NOT /root, NOT /home/<admin>)
sudo -u odoo-semantic -H bash -c '
    cd /opt/odoo-semantic-mcp && make install
'
# Verify:
sudo -u odoo-semantic ls /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python
```

> **uv venv — no `bin/pip`:** `make install` uses `uv` to create the venv. The resulting venv has
> **no** `<venv>/bin/pip`. To add or re-install packages after a `git pull`, use:
> ```bash
> uv pip install \
>     --python /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
>     -e ".[dev]"
> ```
> The `.[all]` extra does not exist — use `.[dev]` (development deps) or `.[integration]`
> (integration-test deps). See ADR-0027 §5.
>
> ⚠️ **MANDATORY after every source update (git pull / new release):** reinstall the package
> into the prod venv BEFORE restarting services — otherwise `importlib.metadata` still reads
> the old `dist-info` and `/health`, `/ready`, `/api/site-config` report a stale version:
> ```bash
> sudo -u odoo-semantic \
>     uv pip install \
>         --python /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
>         -e /home/odoo-semantic/odoo-semantic-mcp   # canonical layout; /opt for legacy (see §3.1 note)
> # Then restart services as usual:
> sudo systemctl restart odoo-semantic-mcp odoo-semantic-webui
> ```

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

> **WARNING - pgvector on managed or split-tier Postgres:**
> `docker compose up` automatically runs `docker/initdb.d/01-pgvector.sql`
> (which issues `CREATE EXTENSION vector;`) via the Docker init mount. This
> does NOT happen when Postgres runs outside docker-compose - for example when
> using a managed service (Amazon RDS, Google Cloud SQL, Azure Database for
> PostgreSQL) or a separately provisioned Postgres host in a split-tier
> deployment.
>
> In those cases you MUST run the following as a superuser **before**
> `python -m src.db.migrate`:
>
> ```sql
> CREATE EXTENSION IF NOT EXISTS vector;
> ```
>
> If the extension is missing, `migrate` silently skips the `embeddings` table
> (a stderr warning is printed, not an error). The migrate run appears to
> succeed, but all ANN-backed tools (`find_examples`, `suggest_pattern`,
> `find_style_override`, `find_deprecated_usage`) will fail with a query error
> at runtime.
>
> **Verify after migrate:**
> ```bash
> psql "$PG_DSN" -c "SELECT to_regclass('public.embeddings');"
> # Must return  public.embeddings  (not NULL).
> # NULL means the table was skipped -- re-create extension and re-run migrate.
> ```
>
> To make `migrate` fail loudly (exit 1) instead of silently skipping the
> embeddings table, set `REQUIRE_PGVECTOR=1` before running:
> ```bash
> sudo -u odoo-semantic -H bash -c '
>     export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
>     export REQUIRE_PGVECTOR=1
>     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
> '
> ```
> This is the recommended setting for production managed-Postgres deployments.

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

### 3.3.5 Profiles and repos setup

`python -m src.db.migrate` áp dụng schema migrations và seed 12 root profile
`odoo_N` (Odoo CE v8–v19) qua migration `0004` (idempotent `ON CONFLICT (name)
DO NOTHING`). Python seeder (`seed_all()`) cũng chạy nhưng **no-op mặc định** —
không bundle roster nào. Admin tạo các profile khác + toàn bộ repos qua web UI
hoặc JSON API.

Xem [`docs/deploy/master-data-upgrade.md`](deploy/master-data-upgrade.md) cho
hướng dẫn tạo profiles, thêm repos, và setup profile hierarchy (delta model per
ADR-0016). Đối với deployment hiện hữu upgrading từ version có seed roster cũ,
xem phần *Upgrading an Existing Deployment* trong cùng doc đó.

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

### 3.5 systemd services (MCP + FastAPI + Astro)

> User `odoo-semantic` đã tạo ở §1.1. M8 ship **3 unit files**.

Repo ship unit files ở **`docs/deploy/`** (canonical path — không có file nào ở `systemd/`).

`install.sh --systemd` tự động cài đặt và điều chỉnh đường dẫn theo ngữ cảnh.
**Idempotent** (issue #144 fix): nếu unit body đã cài có divergence với template
shipped, script SKIP thay vì silent overwrite. Dùng `--force-overrides` để
override.

- **Chạy với sudo (production):** dùng ADR-0027 canonical paths
  (`User=odoo-semantic`, `/home/odoo-semantic/odoo-semantic-mcp`,
  `/home/odoo-semantic/etc/`). Operator trên legacy `/opt/` layout dùng
  drop-in overrides — xem [`docs/deploy/install-runbook.md`](deploy/install-runbook.md).
- **Chạy không sudo (dev workstation):** tự thay `User=<current-user>`,
  `WorkingDirectory=<cwd>`, venv `~/.venv/odoo-semantic-mcp`, và
  `EnvironmentFile=-<cwd>/.env`. File đã điều chỉnh được lưu vào `/tmp/`
  để review trước khi copy thủ công.

```bash
# Production (cần sudo):
sudo bash install.sh --systemd

# Dev workstation (single user, không cần tạo user odoo-semantic):
bash install.sh --systemd
# → in summary + lưu file vào /tmp/; copy thủ công nếu cần quyền root:
#   sudo cp /tmp/odoo-semantic-*.service '/tmp/osm-alert@.service' /etc/systemd/system/
#   sudo systemctl daemon-reload
```

Nếu bạn muốn cài thủ công:

| File | Service | Bind | Cần |
|------|---------|------|-----|
| `docs/deploy/odoo-semantic-mcp.service` | MCP server (port 8002) | `127.0.0.1` qua proxy tier | INI config |
| `docs/deploy/odoo-semantic-webui.service` | FastAPI JSON API (port 8003) | `127.0.0.1` | INI config + `webui.env` (FERNET_KEY) |
| `docs/deploy/odoo-semantic-astro.service` | Astro SSR frontend (port 4321) | `127.0.0.1` | Node.js 22+; `site/dist/server/entry.mjs` pre-built |

#### Bước 0 — Build Astro frontend

Phải build **trước** khi start `odoo-semantic-astro.service`:

```bash
cd /opt/odoo-semantic-mcp/site
pnpm install --frozen-lockfile
pnpm build
# → artifacts tại site/dist/server/entry.mjs (production SSR bundle)
```

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

Cài FastAPI Web UI unit (cần FERNET_KEY — xem §12 để generate):

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-webui.service \
        /etc/systemd/system/

# Tạo file secrets riêng cho Web UI — KHÔNG commit, mode 600:
sudo install -o odoo-semantic -g odoo-semantic -m 600 /dev/null \
    /home/odoo-semantic/etc/webui.env
sudo tee /home/odoo-semantic/etc/webui.env > /dev/null <<EOF
FERNET_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
WEBUI_SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
EOF

sudo systemctl enable --now odoo-semantic-webui
sudo systemctl status odoo-semantic-webui
```

> **Canonical env path (ADR-0027):** `/home/odoo-semantic/etc/webui.env` (owned by the
> `odoo-semantic` system user, under its `$HOME/etc/`). Using `/etc/odoo-semantic/webui.env`
> is only valid if you also add `ReadWritePaths=/etc/odoo-semantic` to the unit — without
> it, `ProtectHome` / `ProtectSystem` will silently hide the file and the unit will start
> without the FERNET_KEY (hard-fail or missing key, depending on LoadCredential vs env-file
> mode). The `odoo-semantic-webui.service` template ships with the `/home/odoo-semantic/etc/`
> path pre-configured.

Cài Astro unit (sau khi `pnpm build` đã chạy — xem Bước 0):

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-astro.service \
        /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-astro
sudo systemctl status odoo-semantic-astro
# → ExecStart sẽ chạy: node dist/server/entry.mjs
```

Cài backup unit + alert template (PR #134 resilience wiring):

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-backup.service \
        /etc/systemd/system/
sudo cp "/opt/odoo-semantic-mcp/docs/deploy/osm-alert@.service" \
        /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-backup
# Template unit không enable trực tiếp — systemd tự instance khi
# `OnFailure=osm-alert@%n` chain fires từ 4 main unit.

# Sanity test alert template (không cần wait main service failure):
sudo systemctl start 'osm-alert@dummy.service'
sudo journalctl -u 'osm-alert@dummy.service' --no-pager | tail
# → Expect a line matching: osm-alert: unit=dummy state=failed host=<hostname>
#   (printed by ExecStart= in osm-alert@.service; `%i` resolves to `dummy`,
#   not `dummy.service` — systemd's %i is the instance name without suffix)
```

> `install.sh --systemd` glob `*.service` tự động pick up cả 5 unit (4
> main + 1 template) và **idempotent** — nếu unit body đã cài có divergence
> với template shipped, script SKIP thay vì silent overwrite (caused issue #144).
> Operator có customization local phải dùng **drop-in overrides** thay vì sửa
> body trực tiếp — xem [`docs/deploy/install-runbook.md`](deploy/install-runbook.md)
> và [`docs/deploy/overrides/`](deploy/overrides/). Bước manual ở trên chỉ cần
> khi không dùng `install.sh`. Xem
> [`docs/deploy/db-tier-operations.md §Alert wiring`](deploy/db-tier-operations.md#alert-wiring-onfailureosm-alertn)
> để cấu hình notifier (email, Slack, PagerDuty).
>
> **Drift audit trước deploy:** `make check-systemd-overrides` so sánh installed
> unit body với template shipped và báo divergence. Chạy trước mọi deploy đụng
> `docs/deploy/*.service` để tránh lặp lại outage 2026-05-19 (issue #144).

⚠️ **Backup `webui.env` an toàn (vd password manager).** Nếu mất
FERNET_KEY → mọi SSH private key đã lưu trong DB không giải mã được.
Nếu mất WEBUI_SESSION_SECRET → mọi session đang đăng nhập bị invalidate (vô hại nhưng gây đăng xuất đột ngột).

### 3.5b Web UI auth setup (M7 W16 — bắt buộc trước khi start Web UI)

Web UI (port 8003) yêu cầu đăng nhập với username + password (bcrypt cost=12,
session cookie TTL=8h). Admin phải tạo ít nhất 1 user **trước** khi mở trình duyệt,
nếu không mọi request bị redirect đến `/login` và không có cách đăng nhập.

**Bước 1 — Tạo user đầu tiên (chạy 1 lần):**

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.manager create-webui-user admin
'
# → Prompt: "Password for 'admin':" (nhập + xác nhận)
# → Output: "✓ Web UI user 'admin' created."
```

**Bước 2 — Set WEBUI_SESSION_SECRET trong webui.env** (xem lệnh tạo file ở trên).
Nếu bỏ qua, server sẽ log warning và dùng secret ngẫu nhiên per-restart (session
mất khi restart — acceptable cho dev, không acceptable cho production).

**Recovery — bị lockout:**

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
        -m src.manager create-webui-user admin --reset
'
# → Prompt mật khẩu mới, ghi đè hash cũ trong DB.
```

**Bootstrap trên deploy mới (không bị lockout):** `create-webui-user` chạy trực
tiếp qua CLI ngoài Web UI, không cần session — có thể chạy ngay cả khi Web UI
chưa start.

**Local dev over plain HTTP:** nếu Web UI không qua TLS (e.g. `http://localhost:8003`),
session cookie sẽ bị rejected vì `Secure` flag mặc định. Set `WEBUI_SECURE_COOKIE=0`
trong webui.env **chỉ cho dev local**:

```
WEBUI_SECURE_COOKIE=0    # local dev only — KHÔNG set trong production
```

⚠️ Đừng bao giờ set `WEBUI_SECURE_COOKIE=0` trong production — cho phép session hijacking qua plain HTTP.

Xem logs:

```bash
sudo journalctl -u odoo-semantic-mcp -f
sudo journalctl -u odoo-semantic-webui -f
```

#### Upgrading from earlier versions

Nếu bạn đã copy unit file thủ công vào `/etc/systemd/system/` trước phiên bản này,
kiểm tra xem `EnvironmentFile=` có dấu `-` prefix chưa:

```bash
grep EnvironmentFile /etc/systemd/system/odoo-semantic-webui.service
# Phải thấy:  EnvironmentFile=-/home/odoo-semantic/etc/webui.env
# Nếu thiếu -: sửa thủ công rồi reload
sudo sed -i 's|EnvironmentFile=\([^-]\)|EnvironmentFile=-\1|' \
    /etc/systemd/system/odoo-semantic-webui.service
sudo systemctl daemon-reload
```

Lý do: thiếu dấu `-` khiến systemd fail unit khi `.env` vắng mặt (vd fresh deploy chưa tạo file),
gây vòng lặp restart vô hạn (`Result: resources`). Với `-` systemd bỏ qua nếu file không tồn tại.

### 3.6 Re-index định kỳ (M6 Wave 2 incremental)

**Khuyến nghị — systemd timer** (load `.env` → có sẵn `PG_DSN` + `NEO4J_PASSWORD`; một entry
cron.d KHÔNG load `.env` nên sẽ fail auth trừ khi creds nằm sẵn trong `odoo-semantic.conf`):

```bash
sudo install -d -o odoo-semantic -g odoo-semantic /var/log/odoo-semantic        # nếu chưa có
sudo cp docs/deploy/odoo-semantic-reindex.service \
        docs/deploy/odoo-semantic-reindex.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-reindex.timer
systemctl list-timers odoo-semantic-reindex.timer --no-pager   # verify lần chạy kế
```

Incremental `index-repo --all` chạy hằng đêm 03:30. Monthly `--full --gc` (dọn stale Module
nodes từ rename/move, per ADR-0007) chạy thủ công — vẫn nạp `.env` qua `systemd-run`:

```bash
sudo systemd-run --uid=odoo-semantic --gid=odoo-semantic --pipe --wait \
  -p EnvironmentFile=-/home/odoo-semantic/odoo-semantic-mcp/.env \
  -p Environment=ODOO_SEMANTIC_CONF=/home/odoo-semantic/etc/odoo-semantic.conf \
  --working-directory=/home/odoo-semantic/odoo-semantic-mcp \
  /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all --full --gc
```

> **Alternative — cron.d** (CHỈ khi `odoo-semantic.conf` đã chứa `pg_dsn` + neo4j creds, vì cron
> không load `.env`). Dùng đúng conf path prod + log đã relocate (followup #14):
> ```
> 30 3 * * * odoo-semantic ODOO_SEMANTIC_CONF=/home/odoo-semantic/etc/odoo-semantic.conf \
>     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all --profile-workers 2 \
>     >> /var/log/odoo-semantic/odoo-semantic-reindex.log 2>&1
> ```
>
> **M6 Wave 2 — incremental indexer:** `pipeline._index_repo` so sánh git HEAD với
> `repos.head_sha` stored. Repo unchanged → zero-cost skip. Otherwise `git diff` để
> filter scan results to changed modules only. `--profile-workers N` để index multi-version
> đồng thời (per-profile lock đảm bảo safe). `--full` flag bypass skip cho periodic cleanup.
> Auto-reseed pattern catalogue cũng wire vào pipeline (sha256 sentinel — cheap khi unchanged).
> See `docs/adr/0007-incremental-indexer.md` cho design decisions.

> **Ops - backfill `--full` sau feature release thêm index-time property (ADR-0053, issue #121):**
> Khi một release thêm property mới TÍNH TẠI INDEX-TIME (ví dụ `shortdesc`/`author` =
> module identity card, hoặc `edition` được reclassify OPL-1+Viindoo -> `viindoo`), incremental
> reindex CHỈ chạm module có git-diff nên các module manifest không đổi sẽ giữ `NULL`/edition cũ
> cho tới khi có một lượt `--full`. Sau khi deploy code, chạy MỘT lượt `--full` off-peak để
> backfill (tối thiểu profile chứa tvtmaaddons, vd `standard_viindoo_17`); ghi (`coalesce` ON
> MATCH) đảm bảo lượt reindex sau thiếu key sẽ KHÔNG xoá dữ liệu cũ. Tool degrade gracefully
> trước backfill (thiếu property = ẩn dòng, không lỗi). Label-text + `profile_inspect(method='coverage')`
> là read-time, sống ngay sau deploy (không cần reindex). Verify bằng
> `check_module_exists('l10n_vn_viin_accounting_vninvoice', 17.0)` -> phải show Display name +
> Edition "Viindoo Commercial".

### 3.6b TTL cleanup timer (nightly session/audit hygiene)

`osm-ttl-cleanup.service` + `osm-ttl-cleanup.timer` chạy **hằng đêm** để xóa các row
hết hạn khỏi bốn bảng: `login_attempts`, `email_verifications`, `active_sessions`,
`admin_audit_log`. Giữ DB gọn + tránh bloat theo thời gian.

```bash
sudo cp docs/deploy/osm-ttl-cleanup.service \
        docs/deploy/osm-ttl-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now osm-ttl-cleanup.timer
systemctl list-timers osm-ttl-cleanup.timer --no-pager   # verify lần chạy kế
```

### 3.6c Module.profile backfill — bắt buộc đúng thứ tự (#259)

Bản vá #259 (ADR-0016 D5) sửa lỗi `:Module` nodes bị bỏ trống `profile=[]` nên vô hình
với mọi profile-scoped MCP query. Khi deploy bản vá này lên một instance đã có data,
**phải chạy đúng 3 bước theo thứ tự** — sai thứ tự làm tenant thấy data trống (fail-closed,
KHÔNG sai data, nhưng thiếu module cho đến khi reindex):

1. **Deploy writer fix trước.** Bản binary/code mới (5 MERGE site đều `SET mod.profile`) phải
   live trước, để mọi write SAU đó đã đúng. Trong cửa sổ giữa bước 1 và bước 2, các `:Module`
   chưa stamp bị từ chối với tenant scoped (rỗng, không lộ chéo).
2. **Chạy backfill cypher** (sửa snapshot hiện có, không re-parse, không downtime):
   ```bash
   cat ops/backfill_module_profile.cypher | cypher-shell -u neo4j -p "$NEO4J_PASSWORD"
   ```
   File `ops/backfill_module_profile.cypher` gồm 3 statement `;`-terminated: STEP 1 backfill
   (mutating, union `profile` từ các child qua `DEFINED_IN`), STEP 2 VERIFY (đếm residual
   `profile=[]` còn lại), STEP 3 drill-down (chẩn đoán). Idempotent — chạy lại là no-op.
   **VERIFY (STEP 2) phải gần 0**; phần dư còn lại CHỈ nên là data-only/i18n module (0 child
   `DEFINED_IN`) — những module này backfill KHÔNG sửa được, cần bước 3.
3. **Chạy `--full` reindex off-peak, từng version** (v17 trước, rồi v18/v19). Đây là remedy
   chính thức theo ADR-0016: stamp `profile` cho cả data-only module mà backfill bỏ sót, và
   dọn stale node:
   ```bash
   ... -m src.indexer index-repo --all --full --gc   # xem 3.6 cho systemd-run wrapper đầy đủ
   ```

> Tóm tắt thứ tự bắt buộc: **writer deploy → backfill (`ops/backfill_module_profile.cypher`)
> → off-peak `--full` reindex**. Nếu STEP 3 drill-down trả về bất kỳ row nào (residual module
> CÓ children), đó là D5 violation mới — điều tra trước khi coi backfill hoàn tất.

> **⚠️ `--full` reindex bão hòa shared embedder — chạy ở cửa sổ traffic THẤP NHẤT.**
> Đường indexer embed (sinh chunk → embed → ghi pgvector) **KHÔNG** đi qua query-side
> anti-freeze semaphore (`EMBEDDER_MAX_CONCURRENCY` chỉ gate hot-path MCP query, không gate
> indexer — xem ADR-0046). Một `--full` reindex đẩy embedder rất mạnh; nếu chạy song song với
> live MCP traffic, embedder có thể saturate → live query gặp `EmbedOverloaded` và (theo thiết
> kế) trả "0 results, retry shortly". **Lưu ý:** `find_examples` chỉ rơi xuống lexical fallback
> (#264) khi embedder **hard-down**, KHÔNG khi chỉ overload — nên trong cửa sổ reindex, semantic
> search degrade (0 result) chứ không tự lexical. Khuyến nghị khi reindex:
> - Chọn cửa sổ off-peak thật sự (đêm/cuối tuần), không trùng giờ làm việc của tenant.
> - **Monitor embedder queue depth / `EmbedOverloaded` rate** trong suốt lần chạy (xem
>   `/ready` field `embedder` + Prometheus `/metrics`).
> - Cân nhắc giảm concurrency indexer trong lúc chạy (ít `--profile-workers`/`--max-workers`,
>   xem §3.6) để chừa headroom embedder cho live query.
> - Reindex từng version một (v17 rồi v18/v19) thay vì tất cả cùng lúc — giảm peak embedder load.

### 3.7 tmux fallback (khi không có systemd)

```bash
sudo -u odoo-semantic -H tmux new -d -s odoo-semantic-mcp \
    'ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.mcp'
sudo -u odoo-semantic tmux attach -t odoo-semantic-mcp   # để xem logs
```

### 3.8 RLS read-tier hardening (single-tenant: skip; multi-tenant: mandatory)

Mặc định MCP `:8002` connect Postgres bằng owner role → RLS trên `embeddings` (ENABLE qua
migration m13_004) **không enforce** (owner-superuser bypass). Để bật enforcement thật (MCP
đọc embeddings dưới non-owner role `osm_reader`, bị tenant policy chi phối), chạy cutover
**idempotent** sau khi đã `make install` + migrate (+ tùy chọn index xong):

```bash
sudo ops/rls_cutover.sh
# → in password osm_reader: LƯU vào secrets manager (cùng chỗ FERNET). Cũng nằm ở
#   /home/odoo-semantic/etc/mcp.env (0600) — file CHỈ MCP :8002 load để override PG_DSN.
```

Script tạo `osm_reader` (non-superuser, non-owner, SELECT-only trên embeddings + đúng grants
mà :8002 cần — xem `ops/rls_create_osm_reader.sql`), `FORCE ROW LEVEL SECURITY`, ghi `mcp.env`,
restart MCP, verify (cross-tenant + `/health`), rollback nếu fail. Re-runnable trên mọi host
(install mới / migration). Chi tiết: [runbook §5.14](deploy/reindex-v8-v19-runbook.md) +
[ADR-0034 A5](adr/0034-multi-tenant-pooled-isolation.md).

**When to run this cutover:**

- **Single-tenant deployment:** SKIP this step. The migration arms RLS on the `embeddings`
  table (ENABLE), but without FORCE the owner connection used by MCP bypasses it by design.
  This is safe and intentional - there is only one tenant, so row-level isolation provides
  no additional protection. The `osm_reader` role is not required.

- **Multi-tenant production (read-tier isolation required):** MANDATORY. Without running
  `sudo ops/rls_cutover.sh`, the RLS policy is ENABLED but not FORCED. The owner connection
  that MCP `:8002` uses bypasses RLS entirely, meaning cross-tenant embedding reads are not
  isolated despite the policy being present. Run the cutover before serving multiple tenants.

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

**Security headers (thêm vào server block):**

```nginx
# HSTS — buộc HTTPS cho 1 năm (chỉ dùng sau khi certbot đã verify TLS hoạt động)
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

# Chống clickjacking + MIME sniffing + referrer leak:
add_header X-Frame-Options           "DENY"             always;
add_header X-Content-Type-Options    "nosniff"          always;
add_header Referrer-Policy           "no-referrer"      always;
```

**Port 443 variant** (dành cho public Viindoo instance — expose thêm cổng khác TLS):

```nginx
server {
    listen 443 ssl http2;
    server_name semantic.example.com;

    ssl_certificate     /etc/letsencrypt/live/semantic.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/semantic.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options           "DENY"             always;
    add_header X-Content-Type-Options    "nosniff"          always;
    add_header Referrer-Policy           "no-referrer"      always;

    location /mcp {
        proxy_pass         http://127.0.0.1:8002/mcp;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_buffering    off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    # /install/ is served by Astro :4321 via the catch-all `location /` (M8);
    # it is NOT proxied to MCP :8002. See docs/deploy/nginx-m8.conf for the
    # full route map.
}
```

Xem `docs/deploy/nginx.conf.example` để biết config đầy đủ (pre-M8, MCP-only).

**M8 — sử dụng `docs/deploy/nginx-m8.conf` thay thế.** Template này route:
- `/api/*` → FastAPI :8003 (JSON only)
- `/admin/*`, `/install/`, và `/` → Astro SSR :4321 (landing + admin UI + install page)
- `/mcp`, `/health`, `/ready` → MCP :8002 (unchanged)

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/nginx-m8.conf \
        /etc/nginx/sites-available/odoo-semantic-mcp
# Thay semantic.example.com bằng domain thật, rồi:
sudo nginx -t && sudo systemctl reload nginx
```

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

#### 4.3a SIGNUP_ENABLED — kiểm soát public self-registration (W0)

Cờ `SIGNUP_ENABLED` quyết định người dùng có thể tự đăng ký tài khoản Web UI hay không.
**Default: `False` (invite-only).** Khi `False`, `POST /api/auth/register` và OAuth
new-account path trả `403 Forbidden` — chỉ admin mới tạo được user mới qua `/admin/users`.

Bật self-registration (cho SaaS public signup):
```ini
# odoo-semantic.conf
[webui]
signup_enabled = true
```
hoặc qua env var: `export SIGNUP_ENABLED=1`

> **Phân định OPS vs UI:** Sau khi bật `signup_enabled`, tenant membership và permission
> vẫn phải admin assign thủ công qua `/admin/tenants`. Non-admin user mới signup sẽ có
> `scope = set()` (deny-all write) cho đến khi admin add vào tenant.

#### 4.3b Nginx `/api/` và app-layer admin gate (defense-in-depth)

Nginx hiện tại chỉ reverse-proxy `/api/*` sang FastAPI — **không có `auth_request`** cấp
nginx cho `/api/*`. Admin gate là **app-layer** (`Depends(require_admin)` trong routes.py),
được enforce bởi FastAPI trước khi handler chạy. Đây là design đúng (single enforcement
point, dễ test), nginx là defense-in-depth cho các mối lo khác (rate-limit, TLS, caching).

Nếu muốn thêm IP allowlist cho `/api/*` ở nginx level (defense-in-depth tùy chọn):
```nginx
location /api/ {
    # Optional: allow only known office IPs
    # allow 203.0.113.10;
    # deny all;
    proxy_pass http://127.0.0.1:8003;
    proxy_set_header X-Real-IP $remote_addr;
}
```

> **`PG_READ_DSN` caveat (OPS-SKIP):** Code hiện tại CHƯA đọc biến `PG_READ_DSN` riêng.
> Nếu admin muốn tách read-DSN cho process MCP (:8002) sang `osm_reader` role (sau khi
> chạy `FORCE ROW LEVEL SECURITY`), cần override `PG_DSN` trong env của service `:8002`,
> KHÔNG đặt `PG_READ_DSN` rồi kỳ vọng có hiệu lực. Xem runbook §5.14.

#### 4.3c TRUSTED_PROXY_CIDRS — bắt buộc khi chạy sau reverse proxy

`/api/waitlist` (và `src/web_ui/login_attempts.py`) thực hiện rate-limit theo IP thật của
client. Khi FastAPI chạy sau nginx, **TCP peer luôn là `127.0.0.1`** — nếu không khai báo
`TRUSTED_PROXY_CIDRS`, mọi request sẽ bị tính chung cho IP `127.0.0.1`, dẫn đến toàn bộ
site bị cap ở `5 request/phút` (rate limit của `/api/waitlist`).

**Bắt buộc** set env var `TRUSTED_PROXY_CIDRS` bằng CIDR của nginx host (hoặc LAN upstream)
để `get_client_ip()` đọc đúng header `X-Forwarded-For`:

```ini
# odoo-semantic-mcp.conf (hoặc systemd EnvironmentFile)
# nginx cùng host:
TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128

# nginx trên LAN khác (vd. HAProxy upstream):
TRUSTED_PROXY_CIDRS=127.0.0.1/32,10.0.1.5/32
```

Sau khi sửa env, restart service FastAPI (`:8003`) và MCP (`:8002`) để pick up thay đổi.

> **Tại sao không để trống?** `TRUSTED_PROXY_CIDRS=` (empty) là safe default cho bare-metal
> (không có proxy) — XFF header bị bỏ qua hoàn toàn. Behind nginx thì PHẢI có giá trị.
> Cross-reference: `src/web_ui/rate_limit.py` + `src/web_ui/login_attempts.py`.

#### 4.3d Phân định OPS vs UI cho M13 hardening

Các bước sau là **OPS production thủ công** (chạy trên server) — KHÔNG phải tính năng
UI và KHÔNG phải gate để mở Web UI W0-W4:

| OPS item | Runbook | Trạng thái |
|---|---|---|
| `FORCE ROW LEVEL SECURITY` + `osm_reader` role | §5.14 | `[x]` done 2026-05-26 |
| Tách read-DSN MCP sang `osm_reader` | §5.14 | `[x]` done 2026-05-26 |
| FERNET_KEY vào `/etc/credstore/` (LoadCredential) | §12 Option B | `[x]` done 2026-05-26 |
| Reindex v8→v19 + cleanup_absolute_path_nodes | §5.11 + §3b | `[ ]` pending |
| MED-2 forge known_hosts cho self-hosted git | §MED-2 | `[ ]` khi cần |

Xem danh sách đầy đủ tại TASKS.md → "UI Completion — OPS-SKIP (7)".

**Tạo API key (admin):**

```bash
# Via CLI:
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key <name>
# → osm_xxxx... (raw key — hiển thị một lần duy nhất, lưu ngay)

# Via Web UI (http://127.0.0.1:8003/api-keys):
# Dashboard → API Keys → Create API Key → điền tên → Copy raw key ngay
```

**Phân phát key cho user:**
- Gửi raw key qua kênh bảo mật (Bitwarden, 1Password — không qua email plain text) cùng URL `https://<your-domain>/install/` — user dán key vào trang đó để nhận snippet đúng cho từng AI tool (không cần copy-paste từ docs)
- Mỗi user/team nên có key riêng để revoke độc lập nếu cần
- Deactivate key: Web UI → Deactivate, hoặc CLI: `UPDATE api_keys SET active=false WHERE name='<name>';`

**Proxy phải forward header `X-API-Key`** (nginx mặc định không strip header — không cần config thêm nếu dùng `proxy_pass`). Caddy forward tất cả headers mặc định.

**/health bypass auth (liveness probe):** `GET /health` không yêu cầu API key — thuần liveness, không DB I/O, dùng cho load balancer:

```bash
curl http://127.0.0.1:8002/health
# → {"status": "alive", "mcp_tools": 25, ...}
# NOTE: embeddings_total/embeddings_by_chunk_type = null cho đến lần /ready đầu tiên
```

**/ready bypass auth (readiness probe):** `GET /ready` cũng không yêu cầu API key — chạy Neo4j + Postgres ping + `SELECT COUNT(*)` embeddings (cached 60s). Dùng để verify sau deploy và cho monitoring dashboards:

```bash
curl http://127.0.0.1:8002/ready
# → {"status": "ok", "neo4j": "ok", "postgres": "ok", "embeddings_total": N, ...}
```

**/metrics bypass auth — BẮT BUỘC hạn chế theo IP scraper ở nginx:**
`GET /metrics` (Prometheus text format, MCP :8002) cũng nằm trong `_PUBLIC_PATHS`
nên **không yêu cầu API key** — đây là pattern Prometheus chuẩn (exporter không tự
auth; mitigation ở tầng reverse-proxy). Vì `/metrics` lộ thông tin vận hành
(latency embed, throughput) nên **phải** giới hạn truy cập chỉ cho IP của
Prometheus scraper ở nginx; không bao giờ public ra Internet:

```nginx
# Chỉ cho phép Prometheus scraper truy cập /metrics; mọi IP khác → 403.
location = /metrics {
    allow 127.0.0.1;          # local scraper
    allow 10.0.0.0/8;         # ← thay bằng subnet/IP của Prometheus server
    deny  all;
    proxy_pass http://127.0.0.1:8002;
    proxy_set_header X-Real-IP $remote_addr;
}
```

> Lưu ý: phải đặt `location = /metrics` (exact match) TRƯỚC `location /mcp` /
> catch-all để nó không bị nuốt bởi prefix-match khác. Nếu không scrape
> Prometheus thì để nguyên `deny all` (chặn hoàn toàn) là an toàn nhất.

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
| TTL cleanup (nightly) | `sudo journalctl -u osm-ttl-cleanup -n 50` |
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

→ **Pre-launch sign-off + 31 MCP tool verification matrix:** [`docs/deploy/pre-launch-checklist.md`](deploy/pre-launch-checklist.md).

Trước khi expose public internet:

- [ ] `.env` và `odoo-semantic.conf` có quyền `600`, owner `odoo-semantic`
- [ ] `NEO4J_PASSWORD` và `PG_PASSWORD` không phải default `password`
- [ ] Neo4j và PG ports bind `127.0.0.1` (kiểm tra `docker compose ps` — cột Ports)
- [ ] MCP server bind `127.0.0.1` (kiểm tra `odoo-semantic.conf [server] host`)
- [ ] TLS cert valid + auto-renewing (certbot timer: `systemctl status certbot.timer` hoặc Caddy auto)
- [ ] **HSTS verify** — sau khi TLS aktif: `curl -I https://<domain>/health | grep Strict-Transport` → header hien thi
- [ ] **Web UI port 8003 không reachable từ external** — kiểm tra từ host ngoài: `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` → connection refused hoặc timeout
- [ ] `rate_limit_rpm` đã cấu hình trong `odoo-semantic.conf` (xem `[auth]` section) — ngăn DoS per API key
- [ ] **`/etc/credstore/FERNET_KEY` backed up riêng** (mode 0600 root:root — canonical FERNET_KEY source via `LoadCredential`; NOT stored in `webui.env` anymore) — mất key này là không recover SSH keys và TOTP secrets đã lưu trong DB
- [ ] **FERNET_KEY lưu trong secrets manager** (Bitwarden, 1Password, Vault), không chỉ để trên disk plain
- [ ] **Docker daemon không expose TCP socket** — `sudo ss -tlnp | grep 2375` phải trống; daemon chỉ Unix socket `/var/run/docker.sock`
- [ ] **X-API-Key auth active** — `curl https://<domain>/mcp` không có header → HTTP 401 (không bypass được)
- [ ] Auth option đã chọn (IP allowlist / Basic Auth / X-API-Key)
- [ ] Service user `odoo-semantic` là non-login (`shell=/usr/sbin/nologin`)
- [ ] Backup đã được test (restore thử ít nhất 1 lần — xem `docs/deploy/disaster-recovery.md`)
- [ ] Logrotate đã cài cho `/var/log/odoo-semantic-reindex.log` (xem §Log Rotation)
- [ ] Web UI session-auth enabled — first admin created via `create-webui-user`, verify unauth GET /repos → 302 /login (xem ADR-0011 + §3.5b)
- [ ] **`osm-ttl-cleanup.timer` enabled** — nightly cleanup của `login_attempts`, `email_verifications`, `active_sessions`, `admin_audit_log` (xem §3.6b); verify: `systemctl is-enabled osm-ttl-cleanup.timer` → `enabled`

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

MCP server (port 8002) yêu cầu `X-API-Key` header với mọi request trừ `GET /health` (liveness) và `GET /ready` (readiness).

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
**Bind**: `127.0.0.1` only (không expose ra internet trực tiếp — xem ghi chú authentication bên dưới)  
**Access qua**: SSH tunnel hoặc Nginx proxy với IP allowlist

> **Authentication:** Web UI yêu cầu đăng nhập (session login) kể từ M7 W16 / ADR-0011
> (bcrypt cost=12, 8h TTL). Port bound `127.0.0.1`-only và đi qua nginx — đây là defense-in-depth,
> KHÔNG phải thay thế cho session auth. Tạo admin user đầu tiên qua §3.5b trước khi mở service.

### Khởi động

**Production (recommended)** — systemd unit (đã document ở §3.5):

```bash
sudo systemctl enable --now odoo-semantic-webui
sudo systemctl status odoo-semantic-webui
```

Service file ship sẵn ở `docs/deploy/odoo-semantic-webui.service` —
có `EnvironmentFile=-/home/odoo-semantic/etc/webui.env` để load FERNET_KEY
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

⚠️ **KHÔNG expose Web UI trực tiếp ra internet** — Web UI yêu cầu session login (ADR-0011, M7 W16),
nhưng port bound `127.0.0.1`-only + nginx IP allowlist là defense-in-depth bắt buộc.

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

`indexer_jobs` không tự cleanup — nếu trở thành lớn, schedule cron `DELETE`. M6 used idempotent `ALTER TABLE IF NOT EXISTS` for additive schema changes; formal migration tool deferred (see ADR-0001 revision).

---

## 12. SSH Keys (M5)

Web UI có thể generate Ed25519 keypair để clone private Odoo repos.

### Yêu cầu: FERNET_KEY

Private key (SSH + TOTP secret) được encrypt bằng Fernet symmetric encryption.
`src.crypto.get_fernet_key()` là single source of truth — resolution order:

1. **`$CREDENTIALS_DIRECTORY/FERNET_KEY`** — systemd `LoadCredential` (preferred
   cho new deployments; key không vào process env). Webui service only.
2. **`$FERNET_KEY`** env var — delivery method cho `EnvironmentFile=` deployments
   (existing setups không cần thay đổi). **Đây là nguồn duy nhất cho CLI**
   (`src/cli.py` — indexer + `rotate-fernet`): CLI chạy như plain process,
   không có systemd credential access.

**Option A — EnvironmentFile (existing deployments, tiếp tục hoạt động):**

```bash
# Generate key (chạy một lần, lưu an toàn):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Đặt vào webui.env (loaded bởi systemd unit):
echo "FERNET_KEY=<output_above>" | sudo tee /home/odoo-semantic/etc/webui.env
sudo chmod 600 /home/odoo-semantic/etc/webui.env
sudo chown odoo-semantic:odoo-semantic /home/odoo-semantic/etc/webui.env
sudo systemctl restart odoo-semantic-webui
```

**Option B — LoadCredential (WI-7 holistic cut — now the active shipped design):**

The shipped `odoo-semantic-webui.service` and `odoo-semantic-backup.service` now carry
**active** `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY` directives. The CLI
(indexer, `rotate-fernet`, `restore`) is covered by the `osm-fernet-run` wrapper.
FERNET_KEY has been removed from `.env` / `webui.env`.

**Deploy ordering — STRICT (provision credstore BEFORE enabling units):**

```bash
# 1. Create credstore dir (once):
sudo install -d -m 0700 -o root -g root /etc/credstore

# 2. Provision the EXISTING key (do NOT generate new — existing SSH/TOTP secrets
#    encrypted under current key must stay decryptable):
#    Retrieve <current-key> from your current webui.env / .env / secrets manager.
echo "<current-base64-fernet-key>" | sudo tee /etc/credstore/FERNET_KEY > /dev/null
sudo chmod 0600 /etc/credstore/FERNET_KEY
sudo chown root:root /etc/credstore/FERNET_KEY

# 3. Install the wrapper (CLI credential delivery):
sudo install -m 0755 docs/deploy/osm-fernet-run /usr/local/bin/osm-fernet-run

# 4. Remove FERNET_KEY from .env / webui.env (key is now in credstore only):
#    sudo sed -i '/^FERNET_KEY=/d' /home/odoo-semantic/odoo-semantic-mcp/.env
#    sudo sed -i '/^FERNET_KEY=/d' /home/odoo-semantic/etc/webui.env

# 5. Reload + restart (credstore must be provisioned BEFORE this step):
sudo systemctl daemon-reload
sudo systemctl restart odoo-semantic-webui odoo-semantic-backup.timer
```

> ⚠️ **Hard-fail — same as before:** `LoadCredential` with a missing source hard-fails
> the unit at status=243/CREDENTIALS — it is NOT a soft fallback to `EnvironmentFile=`.
> The credstore file MUST exist before the units are enabled/started. Operators on
> env-only deployments may comment the `LoadCredential=` line via a drop-in override;
> `src.crypto` still honors `$FERNET_KEY` env fallback for dev/non-systemd runs.
>
> ⚠️ **24.04/26.04 compatibility:** verified on Ubuntu 24.04 (systemd 255) and
> 26.04 (systemd 259). The `osm-fernet-run` wrapper uses `systemd-run --uid=` which
> requires root — always invoke via `sudo osm-fernet-run ...`.
>
> **CLI usage after cut:**
> ```bash
> # Indexer:
> sudo osm-fernet-run /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python \
>     -m src.cli index --profile ...
> # rotate-fernet:
> sudo OLD_FERNET_KEY=<old> NEW_FERNET_KEY=<new> osm-fernet-run \
>     /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.cli rotate-fernet
> # Backup service already has LoadCredential — just run via systemctl:
> sudo systemctl start odoo-semantic-backup.service
> ```
>
> Note: the /tmp ops scripts that provision the credstore and restart the units
> on prod include gates and rollback — run those scripts rather than the raw commands
> above on production.

Dev mode (chạy `python -m src.web_ui` trực tiếp): export `FERNET_KEY` trong shell hoặc thêm vào `.env` rồi `set -a; source .env; set +a`.

⚠️ **Nếu mất FERNET_KEY**: mọi SSH private key và TOTP secret đã lưu sẽ không giải
mã được. Backup key an toàn (vd password manager). Indexer/MCP
server không cần FERNET_KEY runtime — chỉ Web UI và CLI `rotate-fernet` cần.

**Rotation (WI-7 update — `--old-key`/`--new-key` flags REMOVED):**

```bash
# 1. Backup trước:
python -m src.cli backup --output pre-rotation-$(date +%Y%m%d).tar.gz

# 2. Generate new key:
NEW_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 3. Rotate (dùng env var, không dùng CLI flag):
OLD_FERNET_KEY=<current-key> NEW_FERNET_KEY=$NEW_KEY python -m src.cli rotate-fernet
# Output: Rotated N SSH key(s) + M TOTP secret(s). Total: N+M row(s).

# 4. Cập nhật key (chọn Option A hoặc B ở trên) rồi restart service.
```

Re-encrypt cả `ssh_key_pairs.private_key_encrypted` lẫn `totp_secrets.secret_encrypted`
trong một transaction atomic (ADR-0020 WI-7).

### Generate keypair

1. Truy cập http://127.0.0.1:8003/ssh-keys
2. Nhập tên → Generate
3. Copy public key → thêm vào GitHub/GitLab Deploy Keys
4. Private key được lưu encrypted trong DB

---

## 13. Backup

Daily automated backup cho cả Neo4j + PostgreSQL. Cron schedule + retention policy + restore order chi tiết đầy đủ trong runbook:

→ **[`docs/deploy/disaster-recovery.md`](deploy/disaster-recovery.md)** — Backup strategy, restore commands, RTO estimate, validation queries.

§2.4 trong file này có snippet backup thủ công cho ad-hoc use; DR runbook là canonical cho production cron + restore.

### Backup tmpdir — required on tmpfs hosts

On hosts where `/tmp` is a RAM-backed tmpfs (common systemd default), the backup CLI stages two
large files: a full `pg_dump` output and the resulting `.tar.gz` archive. Both compete for the
same tmpfs, which can exhaust available memory or space on production DBs exceeding a few GB.

**Fix:** add `Environment="TMPDIR=/var/tmp"` to `odoo-semantic-backup.service`:

```ini
[Service]
Environment="ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf"
Environment="TMPDIR=/var/tmp"
```

`/var/tmp` is disk-backed and survives reboots — appropriate for large backup intermediates.
See also ADR-0027 §4.

→ **[`docs/deploy/m7.5-production-fixes.md`](deploy/m7.5-production-fixes.md)** — Hotfix runbook cho 5 P1 issues phát hiện M7.5 verification (2026-05-14): HSTS header, Ollama SSL, CoreSymbol re-index, CLICommand re-index, Web UI 404.

---

## 14. Log Rotation

Cron job `§3.6` ghi vào `/var/log/odoo-semantic-reindex.log` — không giới hạn kích thước mặc định. Cài logrotate để tránh file phình to:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/logrotate.d/odoo-semantic \
        /etc/logrotate.d/odoo-semantic
```

File `docs/deploy/logrotate.d/odoo-semantic` đã có sẵn trong repo với config: weekly, 4 tuần lưu, compress, missingok, notifempty. Log file mới được tạo với quyền `640` owner `odoo-semantic:odoo-semantic`.

Verify logrotate hoạt động:

```bash
sudo logrotate --debug /etc/logrotate.d/odoo-semantic
```

---

## 15. SSH Auto-Clone (M6 Wave 4)

Web UI can auto-clone private Odoo repos via SSH instead of requiring manual `git clone + --local-path`.

### Deploy Key Setup

1. **Generate keypair (admin does once):**
   ```bash
   # Paste this SSH URL into the registry form:
   ssh-keygen -t ed25519 -f /tmp/osm-deploy -N "" -C "odoo-semantic-mcp"
   cat /tmp/osm-deploy.pub  # Add to GitHub Deploy Keys → repo settings
   cat /tmp/osm-deploy      # Web UI: SSH Keys form → create new key
   ```

2. **Web UI UI flow:**
   - Admin: open Web UI, go to "SSH Keys" section
   - Click "Generate new keypair" or "Import existing"
   - Save private key (encrypted via FERNET_KEY)

3. **Register repo with SSH URL:**
   - Admin: go to "Repositories" section
   - Click "Add repository"
   - **URL:** `git@github.com:org/repo.git` or `ssh://git@host/path/repo.git`
   - **SSH Key:** select from dropdown (admin only — see self-service note below)
   - Click "Clone"
   - Web UI polls clone status (`clone_status` column); once done → `local_path` auto-set

> **Self-service portal SSH model (W2, ADR-0038):** Non-admin tenant members add SSH
> repos via the customer portal (`/account/repos`) but **do not select a key** — the
> dropdown is admin-only. The deployment uses a single shared admin-managed
> **access key** (`key_type='access_key'`): the admin publishes its **public** key,
> and each user adds that public key as a read-only deploy key on their own git host.
> A non-admin's add-repo request resolves the shared access key server-side (only the
> server cloner ever decrypts the private key); any client-supplied `ssh_key_id` from a
> non-admin is ignored. If no access key is configured yet, the portal returns an error
> asking the user to contact an admin. (Per-tenant `deploy_key` keypairs — ADR-0034 D7 —
> are a separate, deferred self-service surface and never appear in this dropdown.)

### Project-Local `known_hosts`

Host key verification writes to:
```
~/.local/share/odoo-semantic-mcp/known_hosts
```

**Policy:** `-o StrictHostKeyChecking=yes` against a pre-pinned `known_hosts` bundled for GitHub/GitLab/Bitbucket (no trust-on-first-use; self-hosted forges must be pinned manually — see the reindex runbook §MED-2). Per ADR-0035 D3 (supersedes the old accept-new). Admin can inspect/clear if MITM suspected:
```bash
cat ~/.local/share/odoo-semantic-mcp/known_hosts
```

**Design:** project-local (not system `~/.ssh/known_hosts`) ensures multi-tenant safety and no conflicts with user's personal SSH setup (per ADR-0008 D4).

### Full Clone (No `--depth=1`)

SSH auto-clone uses `git clone --branch <branch> --single-branch <url>` (full history). **Why:** M6 Wave 2 incremental indexer requires full git history to compute `git diff old..new` between commits. Shallow clone would force full reindex on every change, defeating the incremental benefit. Trade-off: large Odoo repos take 3–10 minutes to clone. Handled via background job + Web UI polling (async/await pattern).

---

## 16. Recall Benchmark Setup

The M3 `find_examples` tool uses cosine similarity + Neo4j centrality rerank.
Two test tracks verify ranking quality:

### 11.1 Mock recall (regular CI — no Ollama)

`tests/test_find_examples_recall_mock.py` runs in every CI push (marked
`postgres + neo4j`).  It uses a `ClusterEmbedder` that assigns deterministic
cluster-aware vectors: snippets in the same semantic cluster (tax logic, PDF
report, email confirmation) land near each other in embedding space, while a
query for cluster A gets the exact cluster-A anchor vector.

This test catches regressions in the ranking pipeline (cosine query in
pgvector, centrality rerank coefficient, `_find_examples` output parsing)
without requiring Ollama.

```bash
# Run locally (needs Neo4j + PostgreSQL via testcontainers or docker compose):
pytest tests/test_find_examples_recall_mock.py -v
```

### 11.2 Nightly Ollama-gated recall (real embeddings)

`tests/test_find_examples_recall.py` (marker: `ollama`) runs the 100-query
stratified benchmark (50 VN + 50 EN) against a live Ollama instance with
`qwen3-embedding-q5km` and real indexed data.

**Thresholds:**

| Language | recall@5 threshold |
|----------|--------------------|
| Vietnamese (VN) | ≥ 0.75 (38/50 queries must hit) |
| English (EN) | ≥ 0.80 (40/50 queries must hit) |
| Gap (EN − VN) | ≤ 0.05 |

The thresholds reflect observed quality with `qwen3-embedding-q5km` on
Viindoo 17.0 data (M3 implementation).  If EN recall drops below 0.80
after a model swap or data change, investigate embedding instruction drift
(see `src/embedding/instructions.py`).

**GitHub Actions nightly job (`recall-benchmark`):**

The `.github/workflows/nightly-smoke.yml` job `recall-benchmark` is
**skipped** unless the repository secret `OLLAMA_URL` is set.  To enable:

1. Add repository secret `OLLAMA_URL` → URL of an Ollama instance reachable
   from GitHub Actions runners (e.g. a self-hosted runner with Ollama, or a
   cloud endpoint with `OLLAMA_HOST=0.0.0.0:11434`).
2. Optionally add `OLLAMA_MODEL` secret (default: `qwen3-embedding-q5km`).
3. Ensure the Ollama instance has the model pre-pulled:
   ```bash
   ollama pull qwen3-embedding-q5km
   ```
4. The job requires indexed Viindoo 17.0 data (or any profile with 17.0
   modules for the eval queries to be meaningful).

**Run locally:**

```bash
# Start services
docker compose up -d
python -m src.db.migrate

# Index Viindoo 17.0 with embeddings (once):
python -m src.indexer index-repo --profile viindoo_17

# Run the benchmark:
OLLAMA_URL=http://localhost:11434 \
pytest tests/test_find_examples_recall.py -m ollama -v
```

The test auto-skips if Ollama is not reachable — no manual `--skip` flag
needed.

---

## Bootstrap Admin Settings Catalogue (v0.14.0+)

After `python -m src.db.migrate` (which applies m13_010 / m13_011 / m13_012),
run the pattern backfill script:

```bash
~/.venv/odoo-semantic-mcp/bin/python ops/backfill_patterns.py
```

This migrates 115 curated patterns from `src/data/patterns.json` → the new
`patterns` table. The script is idempotent (ON CONFLICT DO NOTHING); safe to
re-run.

`app_settings` system rows (15 Tier-1 keys) are auto-populated on process
start via the bootstrap hook in `src/web_ui/app.py` lifespan. No manual seed
needed — the hook is try/except non-blocking and falls back to code defaults
if Postgres is unavailable at startup.

`ee_modules` rows (16 entries) are inserted directly in migration m13_011 —
no separate script required.

**Verify after bootstrap:**

```bash
psql "$PG_DSN" -c "SELECT COUNT(*) FROM app_settings WHERE scope='system';"
# expected: 15

psql "$PG_DSN" -c "SELECT COUNT(*) FROM ee_modules;"
# expected: 16

psql "$PG_DSN" -c "SELECT COUNT(*) FROM patterns WHERE NOT soft_deleted;"
# expected: 115
```

**Admin UI access after bootstrap:**

- Super-admin: `/admin/settings` — all 4 categories + plans + EE modules + patterns
- Tenant admin: `/tenant/settings` — `quota.*` keys only (Phase 1)

Cache TTL note: changes take effect within ≤60s per worker (TTL polling).
The UI surfaces this constraint. Cross-worker invalidation via NOTIFY/LISTEN
is deferred to Phase 2 (multi-host deployments).

See [`docs/adr/0042-admin-settings-module.md`](adr/0042-admin-settings-module.md)
for full architecture + Phase 2 roadmap.

---

## Database migrations (squashed baseline)

All schema migrations are squashed into a single baseline `migrations/0001_initial.sql`
(commit `cc7687b`, 2026-06-14). The legacy per-feature files (`m13_001` ... `m13_021`,
including the entire M10B billing set) no longer exist on disk - their DDL is folded into
the baseline (billing tables, `pricing_model`/`min_seats`, withdrawal consent, embedding
provenance columns, public/Viindoo tenant isolation, the `__global__` sentinel, and the
osm_reader grants).

Apply the full schema in one step:
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
```

Existing pre-squash deployments already have these migrations applied and need no action.
After applying the baseline, (re-)grant the least-privilege read-only role:
```bash
psql "$PG_DSN" -f ops/rls_create_osm_reader.sql
```

---

## Enable Paid Checkout (post-KYB)

After Polar KYB onboarding is complete:

1. In Admin Settings (`/admin/settings` → Billing category), set:
   - `billing.paid_checkout_enabled` = `true`
   - `billing.polar_checkout_url_map` = JSON `{"pro": "<polar_checkout_url>", "team": "<polar_checkout_url>"}`
2. Verify the paid CTA appears on `/pricing` and the billing dashboard.
3. Register the webhook endpoint URL in the Polar dashboard: `https://<domain>/api/webhooks/polar`
4. Set `POLAR_WEBHOOK_SECRET` in `webui.env` / systemd `Environment=` before the webhook route goes live.
5. Set `billing.polar_product_map` (JSON `{polar_product_id: plan_slug}`) in Admin Settings.

> **Note:** `billing.paid_checkout_enabled` defaults to `False` — no live payments are accepted until this flag is explicitly set. Legal text is CEO-authorized (PR #224, 2026-06-01); external counsel review is recommended post-launch but is not a blocking deployment prerequisite.
