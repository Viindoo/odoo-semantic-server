# Milestone 5.5 — "Polish Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** snapshot test phải cover toàn bộ output format, không chỉ "1 happy path".
> - **Keep it simple (ETHOS §4.1.3):** stdlib + đã có dep (`tqdm` đã transitive qua `huggingface_hub`? — verify trước khi add). Không thêm rich, click.
> - **Tests trước code:** mỗi task = failing test (snapshot baseline) → run đỏ → implement → run xanh → commit.

**Goal:** Sau M5.5, mọi long-running operation (`indexer`) có progress feedback realtime cho admin; mọi MCP tool (6/6) có output snapshot test bảo vệ format khỏi drift trong PR sau; deferred M5 items (backup, feedback, rate-limit, rotation, logging) hoàn tất.

**Why a separate milestone (vs gom vào M5):** Items defer từ pre-M5 audit và Opus debate có nature khác nhau:
- M5 ships: auth middleware, Web UI, health endpoint, Postgres advisory lock, SSH keygen
- M5.5 nhận: polish (verbose, snapshot), deferred features (backup, feedback loop, rate-limit, logging, FERNET rotation)

Pattern này theo precedent M2.5 "Foundation Wow" — milestone phụ giữa các product feature milestone, gom infra/quality work để main milestone (M5 Product Wow) ship sớm + focus được.

**Tech Stack:** Python stdlib (`logging`) + `tqdm` (kiểm tra `pyproject.toml` xem đã có chưa, nếu chưa thì add). Pytest snapshot pattern đã dùng trong `tests/test_output_snapshots.py`.

---

## Scope (2 items từ pre-M5 audit + 5 items deferred từ M5 Opus debate + landing zone)

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

### Section C — Deferred từ M5 (moved per Opus debate rev 2, 2026-05-09)

- [ ] `src/cli.py`: `backup`/`restore` via subprocess — `pg_dump $PG_DSN > pg_dump.sql` + manual Neo4j note (APOC không required). Mock subprocess trong test. **Moved from M5** (document manual procedure là đủ cho M5 ship; script an toàn hơn sau khi M5 stabilize).
- [ ] **Pattern feedback loop:** `POST /api/feedback` endpoint trong Web UI (không phải MCP tool) + `pattern_feedback` PostgreSQL table + thumbs up/down từ `suggest_pattern` output. **Moved from M5** — cần auth layer M5 ship trước; `pattern_feedback` table schema defer kèm (add vào `src/db/auth_registry.py` + migration).
- [ ] **Rate limiting per API key:** per-minute sliding window cap — DoS protection cho production. Local deploy M5 risk thấp (bind `127.0.0.1`). Implement bằng in-memory counter hoặc Redis nếu multi-process. **Moved from M5**.
- [ ] **FERNET_KEY rotation script:** `python -m src.cli rotate-fernet --old-key OLD --new-key NEW` → re-encrypt tất cả `ssh_key_pairs.private_key_encrypted` rows, bump `key_version`. Deploy.md M5 document manual procedure; script ở đây. **Moved from M5**.
- [ ] **Structured JSON logging:** `logging.config` dictionary config hoặc `structlog` — emit JSON lines cho log aggregator (Loki, CloudWatch). WARN format plain text đủ M5; structured log cho production traffic analysis. **Moved from M5**.

### Section D — Landing zone (M5 debt rollup)

Items phát hiện sau khi đóng M5 PR #16:

- [x] **Browser E2E tests:** `tests/test_web_ui_browser.py` — 24 Playwright headless tests cover toàn bộ Web UI (Dashboard, API Keys, SSH Keys, Repos, Navigation). Landed cùng CI refactor chuyển `services:` → `docker compose up -d` (single source of truth cho local + CI + server deploy).
- [x] **`docs/deploy.md` §4.3 stale:** section heading và nội dung vẫn mô tả "M2.5 — chưa có API key validation" dù M5 đã ship auth. Fixed: auth section rewrite + `/health` bypass note + verify config snippet thêm `X-API-Key` header.
- [x] **`README.md` stale note:** dòng "bỏ header `X-API-Key` (M5 sẽ thêm auth)" trong quickstart. Fixed: thay bằng "giữ header với key tạo bằng `create-api-key`".
- [ ] **`tests/test_mcp_server_config.py` isolation:** monkeypatch `_driver = object()` leak sang `tests/test_mcp_spec_tools.py`. Fix: switch sang `monkeypatch.setattr` (carry-over từ M4.6 — ảnh hưởng khi test order thay đổi).

---

## Out of scope (defer xa hơn)

- **Postgres advisory lock cross-server:** M5 đã ship Postgres advisory lock (single-server). Multi-server HA lock (distributed) defer M6 cùng với incremental indexing.
- **Indexer resume từ partial state:** nếu indexer crash giữa chừng, không có rollback Neo4j transaction boundary. Defer M6 cùng incremental.
- **Sentry / distributed tracing:** observability nâng cao defer khi có production traffic thật.

---

## Acceptance criteria (M5.5 done definition)

1. ✅ `python -m src.indexer --profile X --verbose` in progress bar realtime + module-by-module log INFO.
2. ✅ `tests/test_output_snapshots.py::test_resolve_view_*` exist + pass + cover ≥3 view scenarios (base only, 1 extension, multi-extension).
3. ✅ `python -m src.cli backup --output backup.tar.gz` → file tạo thành công (mock subprocess OK).
4. ✅ `POST /api/feedback` với valid JSON → 200 `{"ok": true}`.
5. ✅ Section D "landing zone" empty hoặc có items đã đóng — không bỏ ngỏ.
6. ✅ `make test` green, `make lint` clean.
7. ✅ TASKS.md M5.5 row `[x]`.

---

## Effort estimate

- Task A1: ~20 min (1 test + 3 lines code)
- Task A2: ~1h (decision + test + wire + TTY check)
- Task B1: ~30 min (seed fixture là phần lớn time; logic test đơn giản)
- Section C deferred items:
  - CLI backup/restore: ~25 min (Haiku)
  - Pattern feedback loop: ~25 min (Haiku)
  - Rate limiting: ~30 min (Haiku)
  - FERNET rotation script: ~20 min (Haiku)
  - Structured logging: ~20 min (Haiku)
- Section D: variable, depends on M5 debt
- **Total core (A1+A2+B1):** ~2h
- **Total with deferred M5 items:** ~4h

---

## Dependencies

- **M5 must complete first** (vì Section C depends on M5 debt rollup; A1+A2+B1 có thể start sớm hơn nếu M5 schedule dài).
- KHÔNG block M5 ship — items này độc lập về data/schema, chỉ touch CLI + test layer.

---

## References

- Pre-M5 audit defer items: PR #7 body (`[IMP] pre-m5: config security + UX hardening + docs`)
- Phương án scope split discussion: chat conversation 2026-05-07
- M2.5 precedent (foundation infra giữa product milestones): `docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md`
