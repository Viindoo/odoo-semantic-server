# ADR-0024 — PATCH Mutation Policy cho Repo & Profile (preserve head_sha + reject mutations on indexed profiles)

**Status:** Accepted  
**Date:** 2026-05-16  
**Milestone:** M9 follow-up — Web UI parity (PR #116)

---

## Context

M9+ Web UI cần PATCH support để admin sửa metadata của repo và profile qua giao diện đồ họa mà không cần SSH vào server hay dùng CLI.

Tuy nhiên, hai entity này mang dữ liệu có tính lan tỏa (ripple-effect) vào Graph DB:

- **Repo** mang `head_sha` — trạng thái incremental indexer (ADR-0007). Naive PATCH reset `head_sha` → lần index tiếp theo full-reindex toàn bộ repo thay vì chỉ diff.
- **Profile** mang `name` và `odoo_version` — lan tỏa vào Neo4j: `Module.profile` là string array chứa tên profile, `Module.odoo_version` là property. Đổi 1 trong 2 → thousands of Module nodes trở thành orphan (data vẫn tồn tại trong Neo4j nhưng không khớp với Postgres record nữa).

Ngoài ra, profile còn tham gia invariant ADR-0016: mọi profile trong cùng ancestor chain phải có cùng `odoo_version`. Đổi version của một profile trung gian → vi phạm invariant với parent hoặc descendants.

Cuối cùng, concurrent PATCH requests có thể tạo race condition nếu chỉ dựa vào application-level pre-check: một request đọc "chưa trùng tên" rồi bị preempt trước khi INSERT, request kia cũng đọc "chưa trùng" và cùng INSERT → `UniqueViolation` từ Postgres constraint.

---

## Decision

### Rule 1 — `PATCH /repos/{id}` preserves `head_sha`

`update_repo()` dùng whitelist SET clause, không bao giờ đặt lại `head_sha`. Chỉ các field sau được phép PATCH:

- `url`
- `branch`
- `ssh_key_id`
- `local_path`

**Lý do:** Xóa + tạo lại repo sẽ reset `head_sha = NULL` → full reindex. PATCH giữ nguyên `head_sha` để incremental indexer chạy tiếp được sau khi admin đổi URL hoặc branch — tiết kiệm vài phút index mỗi lần chỉnh metadata.

### Rule 2 — `PATCH /profiles/{id}` reject thay đổi `name` hoặc `version` khi profile đã được indexed

Điều kiện "đã indexed" = `EXISTS (SELECT 1 FROM repos WHERE profile_id=$id AND head_sha IS NOT NULL)`.

Khi điều kiện này đúng: trả về HTTP 409 với exception type `ProfileIndexedError`.

**Lý do:** Neo4j `Module.profile` lưu name string array (không phải FK). `Module.odoo_version` là plain property. Nếu đổi tên/version profile: tất cả Module nodes đã index vẫn giữ giá trị cũ → orphan. Hai workaround an toàn:

- (a) Delete profile + recreate với tên/version mới → reindex từ đầu.
- (b) Trigger `--full` reindex trước khi đổi (tuy nhiên vẫn còn race window giữa reindex và rename; workaround (a) an toàn hơn).

Các field khác (`description`) không ảnh hưởng Neo4j nên vẫn được PATCH bình thường kể cả khi đã indexed.

### Rule 3 — `PATCH /profiles/{id}` kiểm tra version match với cả ancestor lẫn descendant

Nếu profile có parent: `new_version` phải `== parent.odoo_version`.  
Nếu profile có descendants: tất cả `descendant.odoo_version` phải `== new_version`.

Vi phạm → HTTP 422 `ProfileVersionMismatchError`.

Bảo toàn invariant của ADR-0016: mọi node trong ancestor chain phải cùng `odoo_version`.

### Rule 4 — TOCTOU race protection: pre-check + post-UPDATE exception catch

Pattern áp dụng cho cả `update_repo` và `update_profile`:

1. **Fast-path pre-check** — `SELECT EXISTS(...)` trước khi UPDATE. Nếu vi phạm uniqueness → raise exception ngay (tránh hầu hết duplicate work).
2. **Safety net** — `try/except psycopg2.errors.UniqueViolation` bao quanh câu UPDATE. Nếu concurrent request bypass pre-check → catch `UniqueViolation` và map sang HTTP 409 thay vì để server trả 500.

Hai lớp bảo vệ này đảm bảo safe under concurrency mà không cần distributed lock.

---

## Consequences

### Pros

- Admin tự sửa metadata repo/profile an toàn qua Web UI; không cần SSH hay CLI.
- Data integrity bảo toàn: `head_sha` không bị reset vô tình, Neo4j Module nodes không bị orphan, invariant ADR-0016 không bị vi phạm.
- TOCTOU race handled bởi DB constraint → không cần distributed lock.

### Cons

- Admin muốn **rename** profile đã có data phải delete + recreate (UX friction). Không có in-place rename.
- Nếu admin chỉ muốn sửa `description` của profile indexed, UI phải render form đủ thông minh để chỉ disable các field `name`/`version` thay vì block toàn bộ form.

### Migration

Không cần migration — đây là application-layer guard thuần túy. Postgres schema không thay đổi.

Production survey (2026-05-16): 0 inconsistency tồn tại, 24/26 profiles đã indexed — guard hoạt động ngay khi deploy.

### Future

Cân nhắc cascade rename qua background task: đổi tên profile → auto-trigger `--full` reindex → cập nhật Neo4j `Module.profile` array. Defer M10+ vì cần coordination giữa Postgres update, indexer job queue, và Neo4j write transaction.

---

## Alternatives Considered

1. **Cho phép rename tự do + đánh dấu stale Neo4j nodes** — reject. Neo4j không có FK, không có trigger; stale nodes âm thầm trả kết quả sai trong MCP tools cho đến lần reindex tiếp theo. Thời gian phát hiện là không xác định.

2. **Reset `head_sha` khi PATCH `url` hoặc `branch`** — reject. Phần lớn admin sửa URL typo hoặc đổi branch feature → không nên phạt bằng full reindex. URL/branch change không invalidate existing Module nodes (cùng commit content, chỉ khác remote pointer).

3. **Require explicit `--force-reindex` flag trong PATCH body** — reject. Phức tạp API surface; guard 409 đủ rõ ràng để hướng dẫn admin về đúng workaround.

4. **Optimistic locking via `version` column** — không cần cho use case này. TOCTOU chỉ xảy ra tại uniqueness constraint (tên profile trùng), đã được xử lý bởi pattern Rule 4.

---

## Cross-references

- **ADR-0007** — Incremental indexer + `head_sha` tracking. Rule 1 của ADR này bảo vệ invariant D1 của ADR-0007.
- **ADR-0016** — Profile hierarchy + Neo4j isolation. Rule 2 + Rule 3 bảo vệ invariants của ADR-0016.
- **ADR-0021** — Admin audit log. PR #116 extend audit log với before/after snapshot cho PATCH mutations (không chỉ action name).
