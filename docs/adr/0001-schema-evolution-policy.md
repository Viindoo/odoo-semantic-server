# ADR-0001: Schema Evolution Policy

**Date:** 2026-05-06  
**Status:** Accepted  

## Context

PostgreSQL schema trong `src/db/migrate.py` hiện dùng approach đơn giản: single-string SQL `SCHEMA_SQL` với `CREATE TABLE IF NOT EXISTS` — idempotent, đủ cho M2.5 (add 2 tables mới). M5 sẽ thêm 3 tables mới (`ssh_key_pairs`, `api_keys`, `user_profile_access`) — vẫn OK với approach hiện tại. Nhưng M6 incremental indexing cần ALTER TABLE `repos` (thêm column `last_indexed_commit_sha`) — `CREATE IF NOT EXISTS` không đủ để quản lý schema changes.

## Decision

1. **M2.5–M5: Schema add-only.** Chỉ thêm bảng/index mới. `SCHEMA_SQL` là append-only list `CREATE TABLE IF NOT EXISTS`. `python -m src.db.migrate` re-run an toàn (idempotent). Không ALTER TABLE, không DROP, không sửa cột hiện có.

2. **M6: Adopt migration tool.** Khi cần ALTER TABLE lần đầu, switch sang `yoyo-migrations` (hoặc Alembic nếu team quen Odoo convention tốt hơn). `SCHEMA_SQL` hiện tại trở thành migration `0001_initial.sql`. Migration tool giữ version table (`schema_version`) để skip applied migrations + rollback an toàn.

3. **Rule cho intermediate milestones:** Nếu M5 cần ALTER (ngoài add table) — escalate David và update ADR này trước khi implement.

> **Lưu ý:** `CREATE TABLE IF NOT EXISTS` là idempotent với *tạo bảng mới*, nhưng **không thêm column vào bảng đã tồn tại**.
> Nếu developer thêm column vào `SCHEMA_SQL`, lệnh sẽ **silently no-op** trên DB đã có bảng đó.
> Đây chính là failure mode ADR này muốn ngăn. Quy tắc: thêm column = forbidden cho đến M6 khi có migration tool.

## Consequences

**Positive:**
- Không phức tạp hóa sớm. M2.5–M5 dev nhanh, đơn giản, không overhead migration framework.
- `CREATE IF NOT EXISTS` đủ idempotent cho CI/CD nếu trigger migrate nhiều lần.

**Negative:**
- M6 cần refactor `src/db/migrate.py` khi adopt tool. Effort ~1–2 giờ AI-assisted.
- Schema version tracking bắt đầu từ M6 — không có tracing history trước đó.

**Risk:**
- Developer quên rule → cố ALTER trong M5 → production breakage trên target DB có data.
- Mitigation: ADR này là reference trong `CONTRIBUTING.md` checklist; code reviewer gate.

## Alternatives Considered

1. **SQLAlchemy Alembic ngay từ M2.5** — overkill hiện tại, overhead setup. Reject.
2. **Flyway / Liquibase** — Java/overhead. Reject (team Python).
3. **Raw SQL + manual version tracking** — M6 easy refactor từ SCHEMA_SQL thành tool. Accept (picked).

## Revision History

**Revision 2026-05-11:** M6 Wave 1+2+3+4 implemented additive schema changes via idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` patterns (e.g. `repos.head_sha` Wave 2, `repos.ssh_key_id`/`clone_status`/`clone_error_msg` Wave 4). The original plan to adopt yoyo-migrations during M6 was deferred. Rationale:
- Idempotent ALTER suffices for purely additive changes through M6.
- Adding migration framework introduces operational complexity (state table, downgrade tooling) without immediate benefit since no non-additive change was needed.
- Re-evaluate when first non-additive schema change is needed (e.g. column rename, type change, or constraint tightening), likely M7+. Track in M7 backlog as "Migration tool adoption".
