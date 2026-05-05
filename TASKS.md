# Bảng Theo Dõi Tiến Độ — Odoo Semantic MCP

> **Quy ước trạng thái:**
> - `[ ]` — chưa bắt đầu
> - `[~]` — đang làm (AI agent hoặc human đang xử lý)
> - `[x]` — hoàn thành, đã commit
> - `[!]` — bị blocked (ghi lý do bên dưới)
>
> **Quy tắc cho AI agent:** Trước khi bắt đầu task, đánh `[~]` và commit. Sau khi xong, đánh `[x]` và commit. Không làm nhiều tasks cùng lúc trên cùng file.

---

## Milestone 1 — "First Wow"
**Intent:** Chứng minh giá trị cốt lõi — AI hiểu inheritance chain cross-repo.  
**Outcome:** `resolve_model("account.move", "17.0")` trả về đúng full chain không hallucinate.

- [ ] `docker-compose.yml`: Neo4j + PostgreSQL/pgvector
- [ ] `src/indexer/scanner.py`: git branch detection + manifest discovery
- [ ] `src/indexer/registry.py`: module registry per version
- [ ] `src/indexer/resolver.py`: topological sort + circular dep handling
- [ ] `src/indexer/parser_python.py`: `_name`/`_inherit`/`_inherits`/fields/methods
- [ ] `src/indexer/writer_neo4j.py`: Module/Model/Field/Method nodes + edges
- [ ] `src/mcp/server.py`: `resolve_model` + `resolve_field` + `resolve_method`
- [ ] E2E test: kết nối VS Code + Claude Code, verify kết quả

## Milestone 2 — "View Wow"
**Intent:** Mở rộng semantic awareness sang UI layer.  
**Outcome:** `resolve_view("sale.order.form", "17.0")` trả về đúng XPath overrides + XML skeleton.

- [ ] `src/indexer/parser_xml.py`: views, inherit_id, xpath targets
- [ ] `src/indexer/parser_qweb.py`: template inheritance chain
- [ ] `src/mcp/server.py`: `resolve_view` + merged_structure reconstruction

## Milestone 3 — "Semantic Wow"
**Intent:** Tìm kiếm code theo ngữ nghĩa.  
**Outcome:** `find_examples("compute tax based on partner country")` trả về code thật, dùng được ngay.

- [ ] `src/indexer/embedder.py`: nomic-embed-text via Ollama (offline-first)
- [ ] `src/indexer/writer_pgvector.py`: chunk + store embeddings
- [ ] `src/mcp/server.py`: `find_examples` (hybrid: pgvector ANN + Neo4j rerank)

## Milestone 4 — "Impact Wow"
**Intent:** Full-stack impact analysis từ Python model đến JS component.  
**Outcome:** `impact_analysis("field", "sale.order.amount_total", "17.0")` liệt kê chính xác tất cả thứ bị ảnh hưởng.

- [ ] `src/indexer/parser_js.py`: era-aware (era1: extend, era2: define+include, era3: patch)
- [ ] `src/indexer/writer_neo4j.py`: JSPatch + OWLComponent nodes + PATCHES edges
- [ ] `src/mcp/server.py`: `impact_analysis` + risk_level scoring

## Milestone 5 — "Product Wow"
**Intent:** Đóng gói thành sản phẩm bất kỳ ai deploy được trong dưới 10 phút.  
**Outcome:** `docker compose up -d && odoo-semantic index --version 17.0` → xong.

- [ ] `src/auth.py`: API key middleware + usage log (PostgreSQL)
- [ ] `src/web_ui/`: dashboard + key management + index status (FastAPI + Jinja2)
- [ ] `src/cli.py`: `index` / `backup` / `restore` commands
- [ ] `docker-compose.yml`: hoàn thiện + `.env.example`
- [ ] `install.sh`: non-Docker installation path
- [ ] `README.md`: hướng dẫn setup + kết nối VS Code / Claude Code / Codex / Gemini

## Milestone 6 — "Scale Wow" (Ongoing)
**Intent:** Hỗ trợ toàn bộ ecosystem Viindoo, multi-version, incremental updates.  
**Outcome:** Re-index chỉ mất vài giây. Index đồng thời 16.0 + 17.0 + 18.0.

- [ ] `src/indexer/incremental.py`: git commit hash tracking, skip unchanged modules
- [ ] Multi-version: index song song nhiều versions
- [ ] `src/indexer/version_presets.py`: preset "viindoo-17.0", "viindoo-18.0"
- [ ] OpenUpgrade support: migration path awareness across versions
