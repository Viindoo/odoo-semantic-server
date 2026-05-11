# Odoo Semantic MCP

> **Knowledge engine** hiểu sâu codebase Odoo — inheritance chain, view structure, JS patch —  
> expose qua MCP protocol để mọi AI coding tool đều dùng được.

---

## Vấn Đề Đang Giải Quyết

Khi AI coding tool (Claude Code, Codex, Gemini) làm việc với Odoo, chúng thường:

- Hallucinate tên field, method không tồn tại
- Không biết model `sale.order` được extend bởi bao nhiêu module
- Không trace được XPath override chain của một view
- Không biết đổi field `amount_total` sẽ ảnh hưởng đến những gì

**Odoo Semantic MCP** giải quyết điều này bằng cách index toàn bộ codebase Odoo (cross-repo, cross-version) vào Graph DB + Vector Store, rồi expose qua MCP server để AI tool query được.

---

## Cách Hoạt Động

```
Odoo repos (~/git/*_17.0/)
        │
        ▼  index một lần trên server
┌───────────────────────────┐
│  Indexer Pipeline         │
│  Neo4j + pgvector         │
│  MCP Server :8002         │
└───────────┬───────────────┘
            │  HTTPS / MCP protocol
            ▼
  Claude Code / VS Code / Codex / Gemini
  (user chỉ cần thêm URL vào config — không cài gì)
```

**6 MCP tools:**

| Tool | Hỏi gì |
|------|--------|
| `resolve_model` | Inheritance chain, fields, methods của model |
| `resolve_field` | Type, computed/related, extension chain của field |
| `resolve_method` | Override chain, super() calls của method |
| `resolve_view` | XPath overrides, merged XML skeleton của view |
| `find_examples` | Code examples từ codebase theo ngữ nghĩa |
| `impact_analysis` | Đổi field/method này → ảnh hưởng đến những gì |

> **M1–M5 (available now):** `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `find_examples`, `impact_analysis`, `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point` — 14 tools (Odoo core API lifecycle awareness + curated pattern catalogue + EE confusion guard across v8 → v20+). API key auth + Web UI admin (M5).

---

## Onboard End User (Zero Install)

Người dùng **không cài gì**. Nhận URL + API key từ admin → chọn AI tool:

> 🚀 **Nhanh nhất:** truy cập **https://odoo-semantic.viindoo.com:9999/install/**, dán API key vào, copy snippet cho tool của bạn. Bảng dưới là fallback thủ công (hoặc cho self-host với URL `http://127.0.0.1:8002/install/`).

| AI Tool | Config file | Key property | Verify | Auto-trust |
|---------|------------|--------------|--------|------------|
| **Claude Code** | `~/.claude.json` | `mcpServers[].type: "http"` | `claude mcp list` | [→ allow-list](docs/client-setup.md#claude-code-auto-trust) |
| **Codex CLI** | `~/.codex/config.toml` | `[mcp_servers.x].http_headers` | `codex mcp list` | [→ allow-list](docs/client-setup.md#codex-cli-auto-trust) |
| **Gemini CLI** | `~/.gemini/settings.json` | `mcpServers[].httpUrl` | `/mcp` in CLI | [→ allow-list](docs/client-setup.md#gemini-cli-auto-trust) |
| **VS Code** | `mcp.json` (Command Palette) | `servers[].type: "http"` | Start codelens | [→ allow-list](docs/client-setup.md#vs-code-auto-trust) |
| **Antigravity** | `~/.gemini/antigravity/mcp_config.json` | `mcpServers[].serverUrl` | Refresh panel | [→ allow-list](docs/client-setup.md#antigravity-auto-trust) |

> ⚠️ Mỗi client có **file config và schema khác nhau**. Copy-paste snippet sai client → MCP không load (silent fail, chỉ báo "tool not found" khi gọi).

**→ [Hướng dẫn chi tiết từng client + pitfalls → docs/client-setup.md](docs/client-setup.md)**

### Quick add — Claude Code

```bash
claude mcp add --scope user --transport http odoo-semantic <MCP_URL> \
    --header "X-API-Key: <API_KEY>"
```

> ⚠️ `~/.claude/settings.json` (permissions/hooks) ≠ `~/.claude.json` (MCP servers). Xem [docs/client-setup.md#claude-code](docs/client-setup.md#claude-code) để tránh pitfall phổ biến nhất.

---

### Verify After Install — Natural-Language Prompts

Sau khi add xong, **gõ prompt tự nhiên** dưới đây vào AI tool — agent phải tự pick MCP `odoo-semantic` và gọi `resolve_model`. Nếu agent trả lời chung chung kiểu textbook → MCP **chưa load đúng**, quay lại [docs/client-setup.md](docs/client-setup.md).

**English:**
- *"Using the odoo-semantic tools, show me the full inheritance chain of `sale.order` in Odoo 17.0 — which modules extend it?"*

**Tiếng Việt:**
- *"Dùng odoo-semantic, liệt kê toàn bộ inheritance chain của model `sale.order` trên Odoo 17.0 và cho biết module nào extend nó."*

**Tín hiệu đúng:** cite module name từ index + format cây `├─ … └─` + `Defined in: [<repo>] <module>` + counts cụ thể (`Fields: 148`).

**Tín hiệu sai:** prose dài không có module name thật, không gọi tool nào.

> 💡 **Self-host test**: thay `<MCP_URL>` bằng `http://127.0.0.1:8002/mcp` và làm theo [Local E2E Quickstart](#local-e2e-quickstart) bên dưới.

---

## Local E2E Quickstart

Muốn test MCP local với Claude Code (không cần đợi production deploy)? 5 phút setup:

### 1. Clone + cài deps + bootstrap DB
```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp
make install                     # tạo venv + sao .env.example, odoo-semantic.conf.example
# Sửa .env: điền NEO4J_PASSWORD và PG_PASSWORD (replace <PASSWORD> trong PG_DSN)
# Sửa odoo-semantic.conf: điền [neo4j] password + [postgresql] dsn (khớp với .env)
docker compose up -d             # start Neo4j (:7474, :7687) + PostgreSQL (:5432)
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
```

### 1b. Generate FERNET_KEY + create first API key

```bash
# Generate FERNET_KEY (required for SSH key encryption):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add the output to .env as FERNET_KEY=<value>

# Create your first API key:
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key mykey
# → prints: osm_xxxx... (save this — shown only once)
```

### 2. Đăng ký 1 profile + index 1 Odoo repo
```bash
# Cần sẵn 1 Odoo CE 17 repo local. Nếu chưa có:
git clone --depth=1 -b 17.0 https://github.com/odoo/odoo ~/git/odoo_17.0

# Đăng ký + attach repo + index
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile odoo17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-repo \
  --profile odoo17 --url file://local --branch 17.0 --local-path ~/git/odoo_17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --profile odoo17 --no-embed
# (--no-embed bỏ qua M3 semantic search; cần Ollama nếu muốn dùng find_examples)

# (M4.5+) Index Odoo core API symbols + lint rules + CLI cho version 17.0:
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-core \
  --source ~/git/odoo_17.0 --version 17.0
# Sau bước này: lookup_core_api / api_version_diff / find_deprecated_usage /
# lint_check / cli_help mới có data để query.

# (M4.6+) Seed curated PatternExample catalogue (~50 entries):
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer.seed_patterns
# Hoặc skip embed (chỉ Neo4j nodes, không pgvector):
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer.seed_patterns --no-embed
# Sau bước này: suggest_pattern / check_module_exists / find_override_point
# có data để query.
```

### Multi-version parallel indexing

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all \
    --profile-workers 3 --max-workers 2
```

Indexes 3 profiles in parallel; each profile uses 2 repo-workers internally
(up to 6 threads total). Per-profile Postgres advisory locks ensure no two
indexer runs collide on the same profile.

### 3. Start MCP server
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server
# → Server lắng nghe http://127.0.0.1:8002/mcp
```

### 4. Trỏ Claude Code vào local server

```bash
claude mcp add --scope user --transport http odoo-semantic \
    http://127.0.0.1:8002/mcp \
    --header "X-API-Key: osm_xxxx..."
```

Cho client khác (Codex, Gemini, VS Code, Antigravity): thay `<MCP_URL>` trong [Onboard End User](#onboard-end-user-zero-install) section bằng `http://127.0.0.1:8002/mcp`.

Restart Claude Code, gõ prompt tự nhiên (xem [Verify After Install](#verify-after-install--natural-language-prompts)):
```
Dùng odoo-semantic, resolve model sale.order trên Odoo 17.0
```

### Tool dependencies

| Tool | M1–M2 | M3 Semantic | M4 Impact |
|------|:---:|:---:|:---:|
| `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` | ✓ Neo4j | — | — |
| `find_examples` | — | ✓ Neo4j + PostgreSQL + Ollama | — |
| `impact_analysis` | — | — | ✓ Neo4j |

`find_examples` cần Ollama chạy với model `qwen3-embedding-q5km`. Các tool khác không cần embedder.

---

## System Requirements (Server)

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

## Deploy Server (Admin)

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp
make install                                           # tạo venv + config templates
docker compose up -d                                   # start Neo4j + PostgreSQL
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate # bootstrap schema
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile viindoo_17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --profile viindoo_17
# (M4.5+) Index Odoo core specs (CoreSymbol/LintRule/CLI) per version:
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-core --source <odoo_source> --version 17.0
# (M4.6+) Seed curated pattern catalogue (one-shot, idempotent):
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer.seed_patterns
# (M5+) Generate FERNET_KEY + create first API key:
# echo "FERNET_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key admin
# → prints raw key once — distribute to your team

# (M5+) Optional: start Web UI admin on port 8003 (127.0.0.1 only):
~/.venv/odoo-semantic-mcp/bin/python -m src.web_ui &
# → http://127.0.0.1:8003/
# Production: dùng systemd unit `odoo-semantic-webui.service` ship sẵn
# trong docs/deploy/ — tự động restart + load FERNET_KEY từ webui.env.

~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server  # start MCP server
# Production: systemd unit `odoo-semantic-mcp.service` (cũng trong docs/deploy/).
```

→ Xem [`docs/deploy.md`](docs/deploy.md) để biết cách cấu hình từng tier (DB, App, Proxy), systemd service, nginx/Caddy, TLS, backup, security checklist. Hai topology được cover: **all-in-one** (1 host) và **split-tier** (DB + App + Embedder + Proxy ở host khác nhau).

---

## Tài Liệu

| File | Nội dung |
|------|----------|
| [`docs/client-setup.md`](docs/client-setup.md) | **End-user client setup** — Claude Code, Codex, Gemini, VS Code, Antigravity (snippets + pitfalls đầy đủ) |
| [`docs/deploy.md`](docs/deploy.md) | **Admin deploy guide** — DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
| [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | **Pre-launch signoff** — 10 mục verify + 14 MCP tool sign-off table trước khi mở public |
| [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | **DR runbook** — backup frequency, restore order, step-by-step commands, RTO estimate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Bắt đầu ở đây nếu bạn là developer** — setup, chạy tests, workflow |
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: Graph schema, Indexer pipeline, MCP tools, lộ trình |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Hướng dẫn stack: tại sao mỗi công nghệ được chọn, cách dùng đúng, các bẫy cần tránh |
| [`TASKS.md`](TASKS.md) | Bảng theo dõi tiến độ — cập nhật liên tục khi implement |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — `0001` schema evolution policy, `0002` spec schema policy (M4.5), `0003` pattern storage (M4.6) |
| [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 (TDD, từng bước) |
| [`docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md`](docs/superpowers/plans/2026-05-08-milestone-4-5-spec-wow.md) | Plan M4.5 — index Odoo upstream specs (CoreSymbol/LintRule/CLI) + Phase 0 v8/v9 enablement |
| [`docs/superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md`](docs/superpowers/plans/2026-05-08-milestone-4-6-pattern-wow.md) | Plan M4.6 — pattern catalog + override convention + EE confusion guard |

---

## Trạng Thái Hiện Tại

> Xem [`TASKS.md`](TASKS.md) để biết task nào đang làm và task nào tiếp theo.

**Milestone 1 — "First Wow":** `[x]` Code + auto tests complete — còn manual E2E với Claude Code thật  
**Milestone 2 — "View Wow":** `[x]` Code + auto tests complete — còn manual E2E `resolve_view`  
**Milestone 2.5 — "Foundation Wow":** `[x]` Deploy foundation complete — config + PostgreSQL registry + indexer pipeline E2E-ready  
**Milestone 3 — "Semantic Wow":** `[x]` Code + auto tests complete — còn E2E manual + recall benchmark với Ollama thật  
**Milestone 4 — "Impact Wow":** `[x]` Code + auto tests complete — còn manual E2E `impact_analysis`  
**Milestone 4.5 — "Spec Wow":** `[x]` Code + auto tests complete (5 new MCP tools, 4 spec node labels, v8/v9 unblocked) — còn manual E2E `lookup_core_api` / `cli_help` với Odoo upstream indexed  
**Milestone 4.6 — "Pattern Wow":** `[x]` Code + auto tests complete (3 new MCP tools, 54 curated patterns, Module/Method enrichment, EE confusion guard) — còn manual E2E `suggest_pattern` / `check_module_exists` / `find_override_point` với data thật  
**Milestone 5 — "Product Wow":** `[x]` Complete — API key auth + Web UI admin (port 8003) + Postgres advisory lock + health endpoint + install.sh  
**Milestone 5.5 — "Polish Wow":** `[x]` Complete — indexer `--verbose`/tqdm + test isolation + snapshot anti-drift + CLI backup/restore + FERNET rotation + JSON logging + rate limiting + pattern feedback + **job tracking** (Section F: `indexer_jobs` table + `--job-id` lifecycle + `GET /repos/jobs/{id}/status` + status-badge polling) + **audit fixes** (Section G: feedback API on MCP port, pg_dump password leak, defensive health probe, thread-safe key cache, form `maxlength`, embedder skip notice)  
**Milestone 6 — "Scale Wow":** `[x]` Complete 2026-05-10 — **Wave 1:** env harness + P3 polish (advisory locks, `--max-workers`). **Wave 2:** incremental indexer (git diff skip, force-push fallback, ADR-0007) + auto-reseed patterns + cross-profile parallel `--profile-workers`. **Wave 3:** version_presets + pattern catalogue community (80 entries, ADR-0009) + EE auto-detect + find_override_point cross-version diff. **Wave 4:** SSH auto-clone (ADR-0008), Ed25519 decryption, project-local known_hosts, full clone for incremental compat.  
**Milestone 7 — "Lifecycle Wow":** `[x]` Complete 2026-05-11 — Module rename GC (`--gc` flag, ADR-0007 D5), cross-repo dep change propagation (W14), embedding cost observability (call_count thread-safe, ADR-0010), qualified-name AST scope resolver (W13), yoyo-migrations adoption (W15), Web UI session auth (bcrypt+cookie, ADR-0011), MCP HTTP smoke tests (T1/T2), nightly recall benchmark (R1), go-live docs overhaul (D1 — backup/restore/DR runbook/pre-launch checklist) + final-closeout sweep (USES_CORE_SYMBOL V0→V1 expansion, rerank+risk threshold calibration harness, `default_clone_dir` urlparse fix).  

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Mở [`TASKS.md`](TASKS.md) — tìm milestone đầu tiên có `[ ]` hoặc `[~]`, đó là điểm vào
3. Nếu milestone đó có plan tương ứng trong [`docs/superpowers/plans/`](docs/superpowers/plans/) — follow từng bước. Nếu chưa có plan, đề xuất plan trước khi code.
4. Tuân thủ nguyên tắc **Boil the Lake** + **Ship Wow Product** ở mọi quyết định
