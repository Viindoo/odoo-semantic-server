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

- [x] `docker-compose.yml`: Neo4j + PostgreSQL/pgvector
- [x] `src/indexer/scanner.py`: git branch detection + manifest discovery
- [x] `src/indexer/registry.py`: module registry per version
- [x] `src/indexer/resolver.py`: topological sort + circular dep handling
- [x] `src/indexer/parser_python.py`: `_name`/`_inherit`/`_inherits`/fields/methods
- [x] `src/indexer/writer_neo4j.py`: Module/Model/Field/Method nodes + edges
- [x] `src/mcp/server.py`: `resolve_model` + `resolve_field` + `resolve_method`
- [ ] E2E test: kết nối VS Code + Claude Code, verify kết quả *(auto tests: 56/56 PASSED — 39 unit + 17 integration — chỉ còn manual verify với Claude Code thật)*
- [x] `.github/workflows/ci.yml`: lint + unit tests + integration tests (Neo4j service container)

## Milestone 2 — "View Wow"
**Intent:** Mở rộng semantic awareness sang UI layer + thiết lập anti-drift guard.  
**Outcome:** `resolve_view("sale.view_sale_order_form", "17.0")` trả về đúng XPath overrides + view chain.

- [x] `src/indexer/models.py`: thêm XPathInfo, ViewInfo, QWebInfo, ViewParseResult
- [x] `src/indexer/parser_xml.py`: views, inherit_id, xpath targets
- [x] `src/indexer/parser_qweb.py`: template inheritance chain
- [x] `src/indexer/writer_neo4j.py`: View/QWebTmpl nodes + INHERITS_VIEW/EXTENDS_TMPL edges + indexes
- [x] `src/mcp/server.py`: `resolve_view` + view chain reconstruction
- [x] `tests/test_doc_sync.py`: TASKS.md file guard + stale `[~]` marker guard (anti-drift)
- [x] `tests/test_output_snapshots.py`: MCP output schema contract tests (anti-drift)
- [ ] E2E test: kết nối VS Code + Claude Code, verify `resolve_view` kết quả

## Milestone 3 — "Semantic Wow"
**Intent:** Tìm kiếm code theo ngữ nghĩa.  
**Outcome:** `find_examples("compute tax based on partner country")` trả về code thật, dùng được ngay.

- [ ] `src/indexer/embedder.py`: nomic-embed-text via Ollama (offline-first)
- [ ] `src/indexer/writer_pgvector.py`: chunk + store embeddings
- [ ] `src/mcp/server.py`: `find_examples` (hybrid: pgvector ANN + Neo4j rerank)

## Milestone 4 — "Impact Wow"
**Intent:** Full-stack impact analysis từ Python model đến JS component.  
**Outcome:** `impact_analysis("field", "sale.order.amount_total", "17.0")` liệt kê chính xác tất cả thứ bị ảnh hưởng.

- [ ] `src/indexer/writer_neo4j.py`: TARGETS_MODEL edge (View → Model) — hoãn từ M2, prerequisite để query view ảnh hưởng khi đổi model/field
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

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`README.md`](README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ↓ | [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: schema, pipeline, MCP tools |
| ↓ | [`docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan chi tiết Milestone 1 — bắt đầu ở đây |
