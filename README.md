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

> **M1 (available now):** `resolve_model`, `resolve_field`, `resolve_method`  
> **M2–M4 (planned):** `resolve_view`, `find_examples`, `impact_analysis`

---

## Onboard End User (Zero Install)

Người dùng **không cài gì**. Chỉ cần nhận URL + API key từ admin:

**Claude Code** — thêm vào `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "odoo-semantic": {
      "url": "https://semantic.viindoo.com/mcp",
      "headers": { "X-API-Key": "<key>" }
    }
  }
}
```

**VS Code** — thêm vào settings (MCP extension):
```json
{
  "mcp.servers": {
    "odoo-semantic": {
      "url": "https://semantic.viindoo.com/mcp",
      "headers": { "X-API-Key": "<key>" }
    }
  }
}
```

**Codex / Gemini CLI** — xem hướng dẫn tương ứng của từng tool, cùng cấu trúc URL + header.

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
cp .env.example .env                      # điền NEO4J_PASSWORD, PG_PASSWORD, ...

# 1. Python runtime (venv tạo tại ~/.venv/odoo-semantic-mcp/)
make install
# Hoặc thủ công: uv venv ~/.venv/odoo-semantic-mcp && uv pip install --python ~/.venv/odoo-semantic-mcp/bin/python -e ".[dev]"

# 2. Databases (Docker)
docker compose up -d                      # Neo4j + PostgreSQL

# 3. Index lần đầu — Milestone 5 (chưa implement)
# python -m src.cli index --base-dir ~/git --version 17.0

# 4. Khởi động MCP server (long-running — dùng systemd hoặc tmux)
python -m src.mcp.server                  # lắng nghe tại :8002
```

**Backup / Restore khi chuyển server** *(Milestone 5 — chưa implement):*
```bash
# python -m src.cli backup --out backup-$(date +%Y%m%d).tar.gz
# python -m src.cli restore --from backup-20260505.tar.gz
```

---

## Tài Liệu

| File | Nội dung |
|------|----------|
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Bắt đầu ở đây nếu bạn là developer** — setup, chạy tests, workflow |
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: Graph schema, Indexer pipeline, MCP tools, lộ trình |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Hướng dẫn stack: tại sao mỗi công nghệ được chọn, cách dùng đúng, các bẫy cần tránh |
| [`TASKS.md`](TASKS.md) | Bảng theo dõi tiến độ — cập nhật liên tục khi implement |
| [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 (TDD, từng bước) |

---

## Trạng Thái Hiện Tại

> Xem [`TASKS.md`](TASKS.md) để biết task nào đang làm và task nào tiếp theo.

**Milestone 1 — "First Wow":** `[x]` Auto tests 56/56 PASSED — còn manual E2E với Claude Code thật  
**Milestone 2 — "View Wow":** `[x]` Code complete — 100 tests PASS, còn manual E2E `resolve_view`  
**Milestone 3 — "Semantic Wow":** `[ ]` Chưa bắt đầu  
**Milestone 4 — "Impact Wow":** `[ ]` Chưa bắt đầu  
**Milestone 5 — "Product Wow":** `[ ]` Chưa bắt đầu  
**Milestone 6 — "Scale Wow":** `[ ]` Ongoing  

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Đọc [`TASKS.md`](TASKS.md) — xem milestone nào đang cần làm
3. Đọc plan tương ứng trong `docs/superpowers/plans/` — follow từng bước
4. Tuân thủ nguyên tắc **Boil the Lake** + **Ship Wow Product** ở mọi quyết định
