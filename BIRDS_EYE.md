---
status: draft
scope: project
audience: anyone new to this repo
reads-with:
  - README.md
  - product_brief.md
  - architecture/overview.md
  - roadmap.md
---

# Bức tranh toàn cảnh — odoo-semantic-mcp

File này để đọc **đầu tiên** khi quay lại dự án sau một thời gian, hoặc khi onboard người mới. Không có jargon ngoài 10 thuật ngữ được định nghĩa cuối bài.

---

## 1. Dự án này là gì (3 câu)

Một **server tra cứu code Odoo** cho AI coding assistant (Claude Code, Cursor, …). Thay vì bắt AI đọc nguyên file `sale_order.py` để trả lời câu hỏi, AI gọi một **MCP tool** và nhận lại **đáp án đã pre-compute sẵn** trong database. Giá trị bán: AI trả lời đúng hơn + tốn ít token hơn 10–20 lần.

---

## 2. Ví dụ cụ thể — trước và sau

**Câu hỏi của user gõ vào Claude Code:**
> "Sau khi cài `sale_management` + `sale_margin` + `viin_freight_sale`, method `action_confirm` trên `sale.order` thực sự làm gì?"

### Trước (không có dự án này)

```
Claude Code đọc 4 file:
  addons/sale/models/sale_order.py              ~1800 dòng  →  ~25k token
  addons/sale_management/models/sale_order.py   ~250 dòng   →   ~3k token
  addons/sale_margin/models/sale_order.py       ~180 dòng   →   ~2k token
  tvtmaaddons/viin_freight_sale/models/...      ~400 dòng   →   ~5k token
                                               ─────────────────────────
                                                Tổng ~35k token
```

Rồi AI phải **tự đoán thứ tự override**, tự đoán method nào gọi `super()`. Hay sai.

### Sau (có dự án này)

```
Claude Code gọi: resolve_method(model="sale.order", method="action_confirm")

Nhận lại (~800 token):
{
  "chain": [
    { "module": "sale",                         "calls_super": false, "file": "..." },
    { "module": "sale_management",              "calls_super": true,  "file": "..." },
    { "module": "sale_margin",                  "calls_super": true,  "file": "..." },
    { "module": "viin_freight_sale",            "calls_super": false, "file": "..." }
  ],
  "chain_is_broken": true,
  "warnings": ["chain_is_broken: viin_freight_sale does not call super()"]
}
```

**Kết quả**: tiết kiệm ~34k token (~97%), và AI thấy ngay `viin_freight_sale` quên gọi `super()` → bug tiềm tàng được flag luôn. Đây là giá trị bán của sản phẩm.

---

## 3. Nó hoạt động thế nào (sơ đồ block)

```
┌─────────────┐         ┌──────────────────────────────────────┐
│   AI client │ ──MCP──▶│        odoo-semantic-mcp             │
│ Claude Code │         │                                      │
│  Cursor…    │ ◀──JSON─│  ┌──────────┐    ┌─────────────────┐ │
└─────────────┘         │  │ Indexer  │───▶│  PostgreSQL 16  │ │
                        │  │ (libcst) │    │  + pgvector     │ │
                        │  └──────────┘    │                 │ │
                        │       ▲          │ modules         │ │
                        │       │          │ models          │ │
                        │       │          │ fields          │ │
                        │       │          │ methods         │ │
                        │       │          │ views           │ │
                        │       │          │ cache_metadata  │ │
                        │       │          └────────┬────────┘ │
                        │       │                   │          │
                        │  ┌────┴─────┐             │          │
                        │  │ Odoo src │             │          │
                        │  │  .py/.xml│             ▼          │
                        │  └──────────┘    ┌────────────────┐  │
                        │                  │ FastMCP server │◀─┼── MCP
                        │                  │  3 tools P1:   │  │    stdio/http
                        │                  │ resolve_model  │  │
                        │                  │ resolve_field  │  │
                        │                  │ resolve_method │  │
                        │                  └────────────────┘  │
                        └──────────────────────────────────────┘
```

**2 luồng chính**:

- **Indexing (offline)**: đọc source Odoo → parse libcst → tính override chain → ghi Postgres. Chạy khi code thay đổi (git SHA).
- **Query (online)**: client gọi MCP tool → server query Postgres → trả JSON. Đồng bộ, không job nền.

---

## 4. Ai xài

| Actor | Xài để làm gì | Trả phí? |
|---|---|---|
| **Dev Viindoo nội bộ** | Viết module `viin_*` nhanh hơn, ít bug override | Miễn phí nội bộ |
| **Khách hàng BYOC** (Viindoo ecosystem) | Index code riêng của họ kèm Odoo CE → AI hiểu custom code | **$10/project/tháng** trên Hetzner |
| **Community OSS self-host** | Tự chạy Docker Compose trên máy mình | Miễn phí (OSS) |

Break-even Hosted tier: ~3 customer/server → dễ dàng lãi nếu có 10+ customer.

---

## 5. Đang ở đâu (hôm nay: 2026-04-22)

```
Gate 1 (Design confirmed) ✅ passed 2026-04-22

Phase 1 — Python model graph (3 weeks target)
  WP-1  Repo bootstrap + tooling ........... ✅ DONE
  WP-2  Postgres schema + migrations ....... ✅ DONE
  WP-3  Manifest scanner + load-order ...... ✅ DONE
  WP-4  libcst Python parser ............... ✅ DONE
  WP-5  Override-chain resolver ............ ✅ DONE
  WP-6  Indexer driver + cache delta ....... ✅ DONE
  WP-7  Test fixture corpus ................ ✅ DONE
  WP-8  FastMCP server + 3 handlers ........ ✅ DONE
  WP-9  Accept test (10 questions) ......... ⏳ NEXT
  WP-10 Docker Compose dev topology ........ ⏳
  WP-11 Benchmark + exit-criteria report ... ⏳
  WP-12 Tailscale tenant ADR ............... ⏳ (tracking-only)

Gate 2 (Ship ready)       — chờ WP-9..11
Phase 2 (XML view resolver) — chưa bắt đầu

Tests: 227 passed · ruff clean · mypy clean (21 source files)
Code: 21 Python file trong osm/ + scripts/ + migrations/ (~4000 LOC)
Git: CHƯA COMMIT — bundle sẵn khi yêu cầu
```

---

## 6. Lộ trình 4 phase (16 tuần MVP)

| Phase | Tuần | Ship gì | Giá trị chính |
|---|---|---|---|
| **P1** | 1–3 | 3 MCP tool `resolve_model/field/method` | Hiểu Python model của Odoo |
| **P2** | 4–6 | `resolve_view` | Hiểu override XML view |
| **P3** | 7–8 | `find_examples` + BYOC pilot đầu | Bật semantic search, mở doanh thu |
| **P4** | 9–12 | `impact_analysis` | AI review refactor xong còn quét cross-module |
| **P5** | 7–16 song song | Docker / CLI / doc site | Public OSS launch |

Mỗi phase có **correctness floor + token-reduction target** — phải pass cả hai mới qua gate. Xem `roadmap.md` cho bảng chi tiết.

---

## 7. Đọc file nào khi nào

Theo mục tiêu của anh ngay lúc này:

| Anh muốn… | Đọc file |
|---|---|
| Pitch 2 phút cho người ngoài | [README.md](README.md) |
| Hiểu ý tưởng gốc + business model | [product_brief.md](product_brief.md) |
| Hiểu kỹ thuật level cao (C4 diagram) | [architecture/overview.md](architecture/overview.md) |
| Biết khi nào ship cái gì | [roadmap.md](roadmap.md) |
| Biết 1 tool cụ thể làm gì, schema in/out | [specs/resolve_model.md](specs/resolve_model.md) / `resolve_field.md` / `resolve_method.md` |
| Schema DB từng bảng | [data-model/](data-model/) |
| Tại sao chọn Postgres / Voyage / schema-per-tenant | [decisions/](decisions/) (ADR-0001 → 0004) |
| Tại sao parser đúng (bằng chứng code Odoo) | [research/odoo-internals.md](research/odoo-internals.md) |
| **Kế hoạch Phase 1 chi tiết + status từng WP** | [tasks/phase-01-plan.md](tasks/phase-01-plan.md) |
| Checklist done/pending ngắn | [tasks/todo.md](tasks/todo.md) |
| Bài học trong lúc làm | [tasks/lessons.md](tasks/lessons.md) |
| Định nghĩa thuật ngữ | [glossary.md](glossary.md) |
| Quy tắc đóng góp | [CONTRIBUTING.md](CONTRIBUTING.md) |

**Code nằm đâu**:

- `osm/indexer/` — scanner + parser + resolver + driver (WP-3..6)
- `osm/server/` — FastMCP app + 3 handler (WP-8)
- `scripts/` — CLI entry (index, migrate, create_tenant, regenerate_golden)
- `migrations/` — SQL migration per-schema
- `tests/` — 227 test hiện tại

---

## 8. 10 thuật ngữ hay gây rối

| Thuật ngữ | Dịch đời thường |
|---|---|
| **MCP** (Model Context Protocol) | Giao thức chuẩn Anthropic để AI client gọi tool bên ngoài. Tương tự REST API cho AI. |
| **Tool** | Một function AI có thể gọi. VD `resolve_model("sale.order")` là 1 tool call. |
| **Override chain** | Thứ tự các module đè lên định nghĩa gốc. Ví dụ: `sale → sale_management → viin_freight_sale` đều sửa `action_confirm` → chain 3 mắt xích. |
| **`_inherit`** | Extension cùng model (module sau đè module trước). Điển hình. |
| **`_inherits`** | **Khác** `_inherit`. Delegation: child có FK tới parent, xài field của parent như của mình. VD `product.product._inherits={'product.template': 'product_tmpl_id'}`. |
| **Indexed SHA** | Git commit SHA mà index được build lên đó. Mọi response đều trả SHA này để client biết data "fresh" đến đâu. |
| **BYOC** | Bring Your Own Code. Khách hàng Viindoo point server vào repo private của họ → index thêm cạnh Odoo CE. |
| **Tenant** | 1 schema Postgres riêng cho 1 khách (schema-per-tenant). `public` schema chứa Odoo CE dùng chung. Query UNION 2 schema. |
| **libcst** | Thư viện parse Python giữ nguyên whitespace/comment. Dùng nó thay `ast` để snippet trả về byte-accurate. |
| **FastMCP** | Framework Python build MCP server. Dự án xài `mcp.server.fastmcp` (official Anthropic SDK). |

---

## 9. Khi nào nên đọc lại file này

- **Trước mỗi session dài** với Claude Code — refresh context nhanh.
- **Onboard người mới** — gửi file này đầu tiên.
- **Khi thấy bản thân lạc trôi trong spec / ADR** — quay về đây ré-anchor.
- **Cuối mỗi phase** — update section "Đang ở đâu" + bảng roadmap status.

**Không** đọc lại khi: đang debug 1 bug cụ thể (đọc lessons.md + code), đang viết 1 spec mới (đọc specs/_template.md).

---

*Duy trì file này: update khi (a) qua một gate, (b) đóng một WP/Phase, (c) thêm một ADR ảnh hưởng business model. Không đụng vào khi chỉ sửa code thường.*
