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

Người dùng **không cài gì**. Chỉ cần nhận URL + API key từ admin, rồi chọn AI tool đang dùng dưới đây.

> ⚠️ **URL `https://semantic.viindoo.com/mcp` là placeholder** — chưa public deploy.
> Đợi M5 (Product Wow) để có instance production. Hiện tại bạn có thể self-host
> qua [Local E2E Quickstart](#local-e2e-quickstart) bên dưới.

> **Quy ước trong các snippet:** thay `<MCP_URL>` bằng URL admin gửi (production:
> `https://semantic.viindoo.com/mcp`; local self-host: `http://127.0.0.1:8002/mcp`),
> và `<API_KEY>` bằng raw key (`osm_xxxxxxxx...`) admin tạo qua
> `python -m src.manager create-api-key` hoặc Web UI.

> **Sai lầm chung 80% người mắc:** mỗi client lưu MCP config ở **file khác nhau**
> với **schema khác nhau**. Copy-paste snippet sai client → MCP **không load
> nhưng client cũng không báo lỗi** (chỉ "tool not found" khi gọi). Mỗi section
> dưới đây có canonical add command + JSON fallback + verify command + 1 pitfall
> đặc trưng của client đó.

### Claude Code

Docs: <https://code.claude.com/docs/en/mcp>

Cách 1 — CLI (recommended, official):
```bash
claude mcp add --scope user --transport http odoo-semantic <MCP_URL> \
    --header "X-API-Key: <API_KEY>"
```

Cách 2 — JSON fallback (file `~/.claude.json`, **không phải** `~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "odoo-semantic": {
      "type": "http",
      "url": "<MCP_URL>",
      "headers": { "X-API-Key": "<API_KEY>" }
    }
  }
}
```

Verify: `/mcp` trong session đang chạy, hoặc `claude mcp list` ngoài shell. Phải thấy `odoo-semantic … ✓ Connected`.

⚠️ **Pitfall 1 (rất phổ biến):** `~/.claude/settings.json` (cho permissions/hooks) **≠** `~/.claude.json` (cho MCP servers). README cũ ghi nhầm sang `settings.json` → MCP không bao giờ load. Nếu bạn từng làm theo README cũ: xoá entry `mcpServers.odoo-semantic` khỏi `~/.claude/settings.json`, rồi chạy lại `claude mcp add` ở Cách 1.

⚠️ **Pitfall 2:** Sau khi add phải **restart Claude Code** — entry mới không load runtime.

### OpenAI Codex CLI

Docs: <https://developers.openai.com/codex/mcp>

Edit `~/.codex/config.toml` (CLI `codex mcp add` không có `--header` flag — phải edit TOML trực tiếp):
```toml
[mcp_servers.odoo-semantic]
url = "<MCP_URL>"
http_headers = { "X-API-Key" = "<API_KEY>" }
```

Restart Codex. Verify: `codex mcp list`.

⚠️ **Pitfall:** Phải dùng key `http_headers` (snake_case + plural). Viết `headers = ...` Codex sẽ silently ignore và server không gửi auth header → 401 từ MCP.

### Google Gemini CLI

Docs: <https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md>

Edit `~/.gemini/settings.json` (user-global) hoặc `.gemini/settings.json` (project):
```json
{
  "mcpServers": {
    "odoo-semantic": {
      "httpUrl": "<MCP_URL>",
      "headers": { "X-API-Key": "<API_KEY>" },
      "timeout": 10000
    }
  }
}
```

Restart `gemini`. Verify: `/mcp` trong CLI.

⚠️ **Pitfall:** Property phải là `httpUrl` (không phải `url`). Viết `url` thì Gemini coi là SSE deprecated transport → handshake hang/fail.

### VS Code (built-in MCP, v1.99+)

Docs: <https://code.visualstudio.com/docs/copilot/reference/mcp-configuration>

Command Palette (`Ctrl/Cmd+Shift+P`) → **`MCP: Open User Configuration`** — file `mcp.json` mở ra:
```json
{
  "servers": {
    "odoo-semantic": {
      "type": "http",
      "url": "<MCP_URL>",
      "headers": { "X-API-Key": "<API_KEY>" }
    }
  }
}
```

Click **Start** codelens xuất hiện trên server block, hoặc reload window.

⚠️ **Pitfall:** Top-level key là `servers` (KHÔNG phải `mcpServers` như Claude/Gemini/Antigravity). `type` phải đúng `"http"` (KHÔNG phải `"streamable-http"`). KHÔNG đặt MCP servers vào `settings.json` — phải file `mcp.json` riêng.

### Google Antigravity

Docs: <https://antigravity.google/docs/mcp>

IDE → **Manage MCP Servers → View raw config** — hoặc edit thẳng `~/.gemini/antigravity/mcp_config.json`:
```json
{
  "mcpServers": {
    "odoo-semantic": {
      "serverUrl": "<MCP_URL>",
      "headers": { "X-API-Key": "<API_KEY>" }
    }
  }
}
```

Save → click **Refresh** ở MCP panel.

⚠️ **Pitfall:** Property phải là `serverUrl` (camelCase, không phải `url` hay `httpUrl`). File ở `~/.gemini/antigravity/` (chia sẻ prefix với Gemini CLI nhưng schema khác).

---

### Verify After Install — Natural-Language Prompts

Sau khi add xong, **gõ prompt tự nhiên** dưới đây vào AI tool — agent phải tự pick MCP `odoo-semantic` và gọi `resolve_model` (hoặc tool tương đương). Nếu agent trả lời chung chung kiểu textbook về `sale.order` thay vì cite được module name + odoo_version từ index → MCP **chưa load đúng**, quay lại section của client tương ứng.

**English:**
- *"Using the odoo-semantic tools, show me the full inheritance chain of `sale.order` in Odoo 17.0 — which modules extend it?"*
- *"Resolve the model `sale.order` for version 17.0 and list all fields added by extension modules."*

**Tiếng Việt:**
- *"Dùng odoo-semantic, liệt kê toàn bộ inheritance chain của model `sale.order` trên Odoo 17.0 và cho biết module nào extend nó."*
- *"Trên phiên bản Odoo 17.0, model `sale.order` có những field nào và được kế thừa từ đâu?"*

**Tín hiệu đúng** trong response:
- Cite concrete module name từ index (`sale`, `sale_management`, `viin_sale`, `website_sale`, …)
- Có format cây `├─ … └─` (output canonical của tool)
- Có `Defined in: [<repo>] <module>` và `Inherits from: …` block
- Counts cụ thể như `Fields: 148` / `Methods: 394` (không phải con số tròn ước lượng)

**Tín hiệu sai** — agent đang answer bằng general knowledge:
- Trả lời prose dài về "sale.order is a model in Odoo's sales module …"
- Không có module name từ codebase đã index
- Không có format cây
- Không thừa nhận đã gọi tool nào

> 💡 **Self-host test trước khi prod**: thay `<MCP_URL>` bằng `http://127.0.0.1:8002/mcp`
> và làm theo [Local E2E Quickstart](#local-e2e-quickstart) để chạy MCP server local.

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
| [`docs/deploy.md`](docs/deploy.md) | **Admin deploy guide** — DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
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
**Milestone 5.5 — "Polish Wow":** `[x]` Complete — indexer `--verbose`/tqdm + test isolation + snapshot anti-drift + CLI backup/restore + FERNET rotation + JSON logging + rate limiting + pattern feedback (Wave 4 job-tracking → Section F)  
**Milestone 6 — "Scale Wow":** `[ ]` Ongoing  

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Mở [`TASKS.md`](TASKS.md) — tìm milestone đầu tiên có `[ ]` hoặc `[~]`, đó là điểm vào
3. Nếu milestone đó có plan tương ứng trong [`docs/superpowers/plans/`](docs/superpowers/plans/) — follow từng bước. Nếu chưa có plan, đề xuất plan trước khi code.
4. Tuân thủ nguyên tắc **Boil the Lake** + **Ship Wow Product** ở mọi quyết định
