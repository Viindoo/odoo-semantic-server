# Milestone 5.5 — "Polish Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** snapshot test phải cover toàn bộ output format, không chỉ "1 happy path".
> - **Keep it simple (ETHOS §4.1.3):** stdlib + đã có dep (`tqdm` đã transitive qua `huggingface_hub`? — verify trước khi add). Không thêm rich, click.
> - **Tests trước code:** mỗi task = failing test (snapshot baseline) → run đỏ → implement → run xanh → commit.

**Goal:** Sau M5.5, mọi long-running operation (`indexer`) có progress feedback realtime cho admin; mọi MCP tool (6/6) có output snapshot test bảo vệ format khỏi drift trong PR sau.

**Why a separate milestone (vs gom vào M5):** 4 items defer từ pre-M5 audit có nature khác nhau:
- 2 items production baseline (health endpoint, concurrency lock) → block M5 ship → đẩy vào M5
- 2 items polish (verbose, snapshot test) → KHÔNG block M5 ship → tách ra M5.5

Pattern này theo precedent M2.5 "Foundation Wow" — milestone phụ giữa các product feature milestone, gom infra/quality work để main milestone (M5 Product Wow) ship sớm + focus được.

**Tech Stack:** Python stdlib (`logging`) + `tqdm` (kiểm tra `pyproject.toml` xem đã có chưa, nếu chưa thì add). Pytest snapshot pattern đã dùng trong `tests/test_output_snapshots.py`.

---

## Scope (4 items inherited from pre-M5 audit + landing zone for M5 debt)

### Section A — Indexer observability

#### Task A1: `--verbose` flag + INFO log streaming

- [ ] **Test:** `tests/test_indexer_main.py` thêm test verify `python -m src.indexer --profile X --verbose` set logger level INFO (capture log output, assert có line "Indexing module ...").
- [ ] **Code:** `src/indexer/__main__.py` add argparse flag `--verbose` (default False). Khi True → `logging.getLogger().setLevel(logging.INFO)`. Default WARN để không spam.
- [ ] **Verify:** chạy local `python -m src.indexer --profile odoo17 --verbose` → thấy module-by-module progress.

#### Task A2: `tqdm` progress bar

- [ ] **Decision:** check `pyproject.toml` — nếu `tqdm` đã có (transitive qua `huggingface_hub` hoặc `transformers` dùng cho embedder) → reuse. Nếu chưa → add explicit dep.
- [ ] **Test:** `tests/test_indexer_pipeline.py` thêm test verify `index_profile(progress=True)` accept callback / iterate qua progress hook. Mock callback assert được called N lần (N = total modules).
- [ ] **Code:** `src/indexer/pipeline.py` `_index_repo()` wrap inner loop `for version, modules in modules_by_version.items(): for name in sorted_names:` bằng `tqdm(...)` khi `progress=True` flag truyền vào. Default False (CI / library use).
- [ ] **Wire:** `src/indexer/__main__.py` pass `progress=True` khi `--verbose` set (TTY check: `sys.stdout.isatty()` — không spam progress bar trong CI log).
- [ ] **Verify:** chạy local `python -m src.indexer --profile odoo17 --verbose` → thấy progress bar `[██████░░░░] 6/10 modules`.

### Section B — Anti-drift test discipline

#### Task B1: `resolve_view` output snapshot test

- [ ] **Read:** `tests/test_output_snapshots.py` để hiểu pattern dùng cho 5 tool kia (`resolve_model`, `resolve_field`, `resolve_method`, `find_examples`, `impact_analysis`).
- [ ] **Seed:** fixture cần seed Neo4j với:
    - 1 base view (vd: `view_sale_order_form`) thuộc module `sale`
    - 1 inheriting view thuộc module `viin_sale` với 2 XPath ops
    - 1 inheriting view thuộc module `to_sale_custom` với 1 XPath op
    - TARGETS_MODEL edge tới `sale.order`
- [ ] **Test:** call `_resolve_view("sale.view_sale_order_form", TEST_VERSION)`, assert output match snapshot exact (tree separator `├─`, label "Base in", "Extensions (in apply order)", count XPath ops per layer).
- [ ] **Snapshot baseline:** lần đầu chạy fail → review output → commit baseline. Lần sau bất kỳ ai sửa format `_resolve_view` mà không update snapshot → fail trong CI.

### Section C — Landing zone (reserved)

- [ ] **(reserved)** Tech-debt rollup từ M5: bất kỳ debt nào sinh ra trong M5 implementation (vd: helper duplicated, test coverage gap mới, deploy.md outdated) đẩy vào đây thay vì để rải rác.
- [ ] **Process:** khi đóng M5 PR, audit checklist 5 phút → list debt → add bullet vào section này → priority → execute trong M5.5.

---

## Out of scope (defer xa hơn)

- **Concurrency lock proper (PostgreSQL advisory lock)**: M5 baseline dùng file lock đủ cho 1-server. Multi-server lock defer M6 cùng với incremental indexing.
- **Indexer resume từ partial state:** nếu indexer crash giữa chừng, không có rollback Neo4j transaction boundary. Defer M6 cùng incremental.
- **Sentry / structured logging:** observability nâng cao defer khi có production traffic thật.

---

## Acceptance criteria (M5.5 done definition)

1. ✅ `python -m src.indexer --profile X --verbose` in progress bar realtime + module-by-module log INFO.
2. ✅ `tests/test_output_snapshots.py::test_resolve_view_*` exist + pass + cover ≥3 view scenarios (base only, 1 extension, multi-extension).
3. ✅ Section C "landing zone" empty hoặc có items đã đóng — không bỏ ngỏ.
4. ✅ `make test` green, `make lint` clean.
5. ✅ TASKS.md M5.5 row `[x]`.

---

## Effort estimate

- Task A1: ~20 min (1 test + 3 lines code)
- Task A2: ~1h (decision + test + wire + TTY check)
- Task B1: ~30 min (seed fixture là phần lớn time; logic test đơn giản)
- Section C: variable, depends on M5 debt
- **Total core (A1+A2+B1):** ~2h

---

## Dependencies

- **M5 must complete first** (vì Section C depends on M5 debt rollup; A1+A2+B1 có thể start sớm hơn nếu M5 schedule dài).
- KHÔNG block M5 ship — items này độc lập về data/schema, chỉ touch CLI + test layer.

---

## References

- Pre-M5 audit defer items: PR #7 body (`[IMP] pre-m5: config security + UX hardening + docs`)
- Phương án scope split discussion: chat conversation 2026-05-07
- M2.5 precedent (foundation infra giữa product milestones): `docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md`
