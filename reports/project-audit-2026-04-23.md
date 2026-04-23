---
status: published
scope: reports/project-audit
date: 2026-04-23
context: solo-dev project status — trước khi code WP-15
reads-with:
  - ../tasks/phase-02-plan.md
  - ../tasks/todo.md
  - ../reports/phase-01-exit-criteria.md
---

# Project Status — 2026-04-23

Audit trước khi bắt đầu WP-15. 3 góc song song: code quality, doc consistency, project mechanics.

## 1. Đang ở đâu

| Phase | Status | Evidence |
| --- | --- | --- |
| P1 — Python model graph | Code ship, Gate 1 pass, **Gate 2 chưa pass** | `reports/phase-01-exit-criteria.md` (Docker + reviewer pending) |
| P2 — View resolver | Gate 1 pass 2026-04-22; WP-13 (embed spike) + WP-14 (XML parser) done | `tasks/phase-02-plan.md` |
| P2 còn lại | WP-15 (DOM resolver, L) → WP-16 (handler, M) → WP-17 (accept, M-L) | 10–16 dev-day ước lượng |

**Test suite:** 237 pass / 22 skip (skip vì `DATABASE_URL` + hardcoded `/home/soncrits/...` path không portable osm-dev).

**Commit velocity:** 11 commit trong 3 ngày (22–23/04), nhưng P1 bundle là 1-shot commit gộp 11 WP → velocity thật khó đo. WP-14 ship trong 0.5–1 dev-day vs estimate 2–3 dev-day — under-budget rõ, hoặc driver integration phần WP-14 đã defer sang WP-15 (cần xác nhận).

---

## 2. Tech debt & issue (sorted by severity)

### HIGH — xử trước WP-15

| # | Issue | File / nơi | Fix cost |
| --- | --- | --- | --- |
| 1 | `osm-mcp.service` chạy `nohup &` → WSL reboot chết | `_scratch_server_setup.md §Wrap systemd` | 20 min |
| 2 | 2 local patch chưa commit — `test_schema_diff.py` OID filter + `osm/server/app.py --allowed-host` | git working tree osm-dev | 30 min |
| 3 | Hardcoded `/home/soncrits/...` trong `tests/indexer/test_python_parser_real.py` → 10 test skip trên osm-dev | cùng file | 30 min, thay bằng env `ODOO_SOURCE_PATH` |
| 4 | **Gate 2 P1 chưa chính thức pass** nhưng P2 đã chạy — vi phạm gate enforcement | `reports/phase-01-exit-criteria.md` | 1 ADR "Gate 2 defer tới sau WP-17" HOẶC ship WP-10 Docker + reviewer passes |

### MEDIUM — defer được, nhưng nên nhớ

| # | Issue | File |
| --- | --- | --- |
| 5 | `warnings` duplicate trong envelope response (`result.warnings` + top-level `warnings`) | `osm/server/handlers/resolve_model.py:122,125` và 2 handler khác — sửa 1 lần trước WP-16 rẻ hơn 4 handler sau |
| 6 | `assert fetched is not None` trong hot path — crash silent nếu chạy với `python -O` | `osm/indexer/driver.py:296, 429, 529, 1105` |
| 7 | Không có connection pool trong `--http` mode — `psycopg.connect()` per request | `osm/server/app.py:138` — matter khi WP-17 stress test |
| 8 | `product_brief.md` không đề cập multi-tenant overlay (ADR-0004 accepted nhưng brief chưa update) | `product_brief.md` + open item trong `todo.md` |
| 9 | `BIRDS_EYE.md §5` stale: ngày "2026-04-22" + test count 247 (thật là 237 sau WP-14) | `BIRDS_EYE.md` |
| 10 | `cache_metadata.md` là stub (chỉ frontmatter), trong khi table đã ship + indexer dùng xuyên suốt | `data-model/cache_metadata.md` |
| 11 | `osm/indexer/xml_parser.py` không có size guard cho `arch_xml` — Kanban phức tạp có thể tạo payload lớn | `xml_parser.py` |
| 12 | `migrate.py` có `_SCHEMA_PATTERN` regex riêng thay vì import `tenancy.validate_tenant` — 2 regex có thể drift | `scripts/migrate.py` — lessons 2026-04-22 đã note nhưng chưa thành todo |

### LOW — không động đến ngay

- `specs/find_examples.md`, `specs/impact_analysis.md` là stub (P3/P4 scope, chưa cần)
- P50 numbers khác nhau ở 3 file (README "<50ms", phase-01-plan "<20ms single-entity + <50ms method", BIRDS_EYE "<100ms deep-chain") — không sai nhưng scope không rõ
- Raw DB password ở `/tmp/osm_pwd.txt` (clear khi reboot, không critical)

---

## 3. Doc consistency

Doc system cơ bản nhất quán. 3 drift đáng log:

1. **`product_brief.md` ↔ ADR-0004** — brief chưa mention multi-tenant. Người mới đọc brief → architecture sẽ confused khi thấy `tenant` column khắp nơi.
2. **`BIRDS_EYE.md §5` stale** — date + test count cần update sau mỗi WP close (habit gap, không phải structure gap).
3. **Status `draft` trên `phase-01-plan.md` + `phase-02-plan.md` + `BIRDS_EYE.md`** — không có policy rõ khi nào `draft` → `confirmed` cho task files. Minor, nhưng BIRDS_EYE là entry point cho onboarding, để `draft` gây câu hỏi không cần thiết.

---

## 4. Gate status

| Gate | Status | Notes |
| --- | --- | --- |
| P1 Gate 1 — Design confirmed | ✅ PASS | 14 file confirmed + 3 ADR accepted |
| **P1 Gate 2 — Ship ready** | ❌ **NOT PASSED** | Docker (WP-10) blocked host-side; code-reviewer + security-reviewer pre-commit pending |
| P2 Gate 1 — Design confirmed | ✅ PASS | `specs/resolve_view.md` confirmed, `data-model/views.md` confirmed, schema shipped |
| P2 Gate 2 — Ship ready | ⏳ N/A | WP-15/16/17 chưa xong |

**Gợi ý:** ADR mới `0006-gate-2-p1-deferred.md` với rationale "P1 accept criteria đã pass margin lớn + WP-10 Docker block host-side, không block P2 design/correctness → gate 2 defer tới sau WP-17 rồi bundle cả P1+P2 thành 1 ship-ready event." Đây là giải pháp solo-dev: thay vì chạy 2 gate review tách biệt, bundle 1 lần. Minh bạch, không ngầm.

---

## 5. WP-15 readiness check

Trước khi code WP-15 (DOM-level inheritance resolver, risk `position="replace"` §8a):

- [x] Spec `resolve_view.md` §8a confirmed — có canonical reference `template_inheritance.py`
- [x] `xml_parser.py` + 8 cv_* fixture ship → input cho resolver ready
- [x] phase-02-plan §WP-15 có 11 test scenario rõ
- [ ] **Prereq (optional, low cost):** đóng HIGH #1, #2, #3 trước — 80 min, unblock infra stability + xóa skip test path hardcode
- [ ] **Prereq (tốt hơn là làm):** thêm item vào `todo.md` hoặc WP-15 acceptance: "driver integration `_index_xml_files` đã defer từ WP-14 → confirm làm ở WP-15 hay tách WP-14b"

Đề nghị: làm HIGH #1–#3 trước (chi phí 80 min, loại fragility + portability debt đã biết lâu), rồi mới bắt đầu coding resolver.

---

## 6. Action list ưu tiên

### Tuần này

1. **[20 min]** Wrap `osm-mcp.service` systemd user service — `_scratch_server_setup.md` đã có recipe.
2. **[30 min]** Commit 2 local patch về git (OID filter + `--allowed-host`). Rủi ro mất nếu reset machine state.
3. **[30 min]** Thay hardcoded `/home/soncrits/...` bằng env `ODOO_SOURCE_PATH` trong `test_python_parser_real.py` → unlock 10 test trên osm-dev.
4. **[15 min]** Viết ADR-0006 "Gate 2 P1 deferred to post-WP-17" để đóng gate enforcement gap; HOẶC đóng WP-10 + chạy reviewer passes.
5. **[15 min]** Đóng open item trong `todo.md` về `product_brief.md` + ADR-0004 (thêm 1 đoạn multi-tenant hoặc note "ADR-0004 canonical"). Update `BIRDS_EYE §5` date + test count.

### Bắt đầu WP-15 sau khi 1–5 xong

Theo phase-02-plan Wave 4 (days 5–8 trong kế hoạch gốc). Trong slice đầu WP-15:

- Ship `osm/indexer/view_resolver.py` (pure function) + `tests/indexer/test_view_resolver.py` (11 scenario).
- Tách driver integration `_index_xml_files` thành slice riêng trong WP-15 umbrella (cần DB, ngang hàng complexity).
- Xác nhận `position="replace"` `replaced_ancestor` detection khớp Odoo canonical.

Sau khi WP-15 xanh → WP-16 handler. Trước khi ship handler → sửa issue #5 (`warnings` duplicate trong envelope) một lần cho cả 4 handler.

### Defer sau P2 ship

- Issue #6 (`assert` → `RuntimeError`), #7 (connection pool), #11 (arch_xml size guard), #12 (migrate.py regex dedup)
- Stub doc `cache_metadata.md`, `find_examples.md`, `impact_analysis.md` (P3+ scope)

---

## 7. Cần xác nhận từ founder

1. Confirm driver integration `_index_xml_files` sẽ làm ở WP-15 hay tách WP-14b? (ảnh hưởng scope WP-15)
2. Gate 2 P1 — chọn đường nào: ship WP-10 Docker + reviewer pre-commit, hay ADR-0006 defer bundle với P2?
3. HIGH #1–#3 có OK làm trước WP-15 không? (chi phí 80 min total)

Sau khi đủ trả lời, WP-15 plan (đã soạn trong chat trước) có thể refine rồi code theo wave.
