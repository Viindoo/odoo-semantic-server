# RLS Cutover Runbook

> Bật `FORCE ROW LEVEL SECURITY` trên bảng `embeddings` và chuyển MCP :8002 sang DSN read-only (`osm_reader`). ADR-0034 A5 / WI-7.

## Nguyên lý

Trước cutover, RLS trên `embeddings` chỉ ở trạng thái "armed-but-dormant" (migration m13_004): policy đã được cài đặt, nhưng bị bỏ qua vì MCP :8002 kết nối với tư cách là chủ sở hữu bảng (`odoo_semantic` role). PostgreSQL **không bao giờ** áp dụng RLS cho chủ sở hữu, ngay cả khi có policy — đó là lỗ hổng tính năng của PostgreSQL để cho phép quản trị viên không bị khóa khỏi dữ liệu của chính họ.

RLS enforcement cần hai điều kiện:

1. **Non-owner role**: MCP :8002 phải kết nối với tư cách là `osm_reader` (không phải `odoo_semantic`), một vai trò cơ sở dữ liệu NON-superuser, NON-BYPASSRLS.
2. **FORCE RLS**: Bảng phải có cờ `FORCE ROW LEVEL SECURITY`, buộc PostgreSQL áp dụng policy cho tất cả các vai trò, kể cả chủ sở hữu (đặc biệt hữu ích khi còn các kết nối từ chủ sở hữu dùng để di chuyển hoặc sao lưu).

Cutover này là **atomic + revertible**: nếu bước xác minh nào bất bại, script tự động rollback — xóa `mcp.env` (quay lại DSN chủ sở hữu) và restart MCP. Vai trò `osm_reader` + cờ FORCE được để lại nguyên trạng (vô hại); operator sửa các vấn đề cơ bản và chạy lại.

## Precondition

- Migration `m13_004_embeddings_rls.sql` đã được apply (xác minh: `psql ... -c "SELECT 1 FROM pg_policies WHERE tablename='embeddings' AND policyname='embeddings_tenant"` trả về 1 hàng).
- Migration `m13_006_plans_quota.sql` đã được apply — bao gồm `GRANT SELECT ON plans TO osm_reader` và `GRANT SELECT, INSERT, UPDATE ON usage_counter TO osm_reader`. Nếu chưa apply, chạy `python -m src.db.migrate` trước (xem `docs/deploy/runbooks/post-pr-ops.md §Action 0`).
- Migration `m13_008_waitlist_emails.sql` đã được apply — bao gồm `GRANT SELECT ON waitlist_emails TO osm_reader` (phòng ngừa: future admin viewer page đọc qua MCP không gặp RLS silent-empty bug).
- `ops/rls_create_osm_reader.sql` đã include grants trên `plans` + `usage_counter` — verify bằng: `psql -d $DB_NAME -c "\dp plans" | grep osm_reader` (expect `r` = SELECT) và `"\dp usage_counter" | grep osm_reader` (expect `arw`).
- Container PostgreSQL `<PG_CONTAINER>` đang chạy và accessible từ host (ví dụ: `docker inspect <PG_CONTAINER>` thành công).
- DSN chủ sở hữu (`<DB_OWNER>` user trên `<DB_NAME>`) còn hoạt động và kết nối thành công.
- MCP service `<MCP_SERVICE>` đang chạy với systemd; operator có quyền `systemctl restart`.
- Operator có quyền `root` (để chạy `sudo ops/rls_cutover.sh`, ghi `<MCP_ENV>`, restart systemd).
- Sao lưu gần đây (≤24 giờ) — **strongly recommended** trước mọi lần cutover.

## Placeholder reference — ADR-0027 canonical defaults trên prod

| Placeholder | Canonical default | Mô tả |
|---|---|---|
| `<PG_CONTAINER>` | `odoo-semantic-mcp-postgres-1` | Tên container Docker chạy PostgreSQL |
| `<DB_NAME>` | `odoo_semantic` | Tên cơ sở dữ liệu |
| `<DB_OWNER>` | `odoo_semantic` | Tên role chủ sở hữu (app role, superuser) |
| `<APP_USER>` | `odoo-semantic` | Tên user systemd chạy MCP service |
| `<MCP_SERVICE>` | `odoo-semantic-mcp` | Tên systemd unit cho MCP :8002 |
| `<MCP_ENV>` | `/home/odoo-semantic/etc/mcp.env` | Đường dẫn tệp DSN MCP-only (0600, non-owner) |

**Override bằng env variables** (cách dùng):
```bash
sudo DB_NAME=custom_db PG_CONTAINER=pg-prod-01 APP_USER=mcp-prod \
  ops/rls_cutover.sh
```

## Sequence — Các bước của cutover

Cutover script `ops/rls_cutover.sh` (bắt buộc chạy dưới `sudo`) thực hiện các bước này theo thứ tự:

### Bước 0: Baseline — Đếm embeddings toàn bộ (chủ sở hữu bypass RLS)

Kết nối là `<DB_OWNER>`, do đó RLS được bỏ qua. Script lưu tổng số hàng `embeddings` — con số này sẽ được dùng để xác minh ở bước cuối.

### Bước 1: Tạo/làm mới vai trò `osm_reader` + grants (idempotent)

Script chạy `ops/rls_create_osm_reader.sql` với mật khẩu được tạo hoặc được cung cấp (env `OSM_READER_PASSWORD`). SQL này:

- **Tạo** vai trò `osm_reader` nếu chưa tồn tại, với các thuộc tính: `LOGIN`, `NOSUPERUSER`, `NOINHERIT`, `NOCREATEDB`, `NOCREATEROLE`, `NOBYPASSRLS`.
- **Cấp quyền** cụ thể: `SELECT embeddings` (chỉ đọc), `SELECT/UPDATE api_keys` (auth + session), `SELECT profiles/repos` (kiến trúc), `INSERT usage_log/admin_audit_log` (best-effort logging), `SELECT/INSERT pattern_feedback` + `ssh_key_pairs` (routers :8002).
- **Cấp quyền** trên các sequence backing SERIAL PKs của các bảng `INSERT`.

Bước này **idempotent**: chạy lại là an toàn, chỉ làm mới mật khẩu và assertion thuộc tính.

### Bước 2: FORCE ROW LEVEL SECURITY trên `embeddings`

SQL: `ALTER TABLE embeddings FORCE ROW LEVEL SECURITY;`

Cờ này **không thể** được undo mà không có quyền superuser, nhưng kết hợp với chuyển MCP sang `osm_reader`, nó buộc policy được áp dụng. Đây cũng là **idempotent**: chạy lần thứ hai là no-op.

### Bước 3: Ghi `<MCP_ENV>` (file DSN MCP-only, 0600, owner `<APP_USER>`)

Script tạo hoặc ghi đè file `/home/<APP_USER>/etc/mcp.env`:

```
PG_DSN=postgresql://osm_reader:<password>@<PG_DSN_HOST>:<PG_DSN_PORT>/<DB_NAME>
```

File này **chỉ** được đọc bởi MCP :8002; webui, indexer, backup không tải nó (vẫn dùng DSN chủ sở hữu từ biến env chính). Chmod `0600` (read-only owner) — chỉ `<APP_USER>` có thể đọc.

### Bước 4: Restart MCP

`systemctl restart <MCP_SERVICE>` — tải `<MCP_ENV>` mới và khởi động lại kết nối.

Downtime dự kiến: 30–60 giây (restart sạch, reconnect).

### Bước 5: Xác minh

Xác minh bao gồm 4 kiểm tra:

1. **Service liveness**: `systemctl is-active <MCP_SERVICE>` = `active` (retry polling, tối đa 15s).
2. **Health endpoint**: `GET /health` trả về JSON; `embeddings_total` **phải bằng** baseline từ bước 0 (đảm bảo read-tier thấy full count).
3. **Sanity grants**: SQL query xác minh `osm_reader` có đúng thuộc tính: `SELECT embeddings`, `NOT INSERT embeddings`, `SELECT api_keys`, `NOT SUPERUSER`, `NOT BYPASSRLS`.
4. **Cross-tenant smoke** (nếu có ≥2 profiles với dữ liệu):
   - Chọn 2 profiles hàng đầu theo dữ liệu: `PA` (chủ sở hữu), `PB` (khác).
   - Kết nối là `osm_reader` với GUC `app.allowed_profiles = 'PA'`.
   - Query rows của `PA` → expect `>0` (chủ sở hữu thấy dữ liệu của mình).
   - Query rows của `PB` → expect `0` (cấm RLS).
   - Fresh install (<2 profiles)? Smoke test bị skip — vẫn coi là pass (test không áp dụng).

Nếu **bất kỳ** kiểm tra nào fail:

- Script **tự động rollback**: xóa `<MCP_ENV>` + `systemctl restart <MCP_SERVICE>` (quay lại DSN chủ sở hữu).
- Vai trò `osm_reader` + cờ FORCE để lại nguyên trạng (vô hại).
- Script thoát với mã lỗi 1 → operator kiểm tra vấn đề (xem **Troubleshooting** bên dưới) + chạy lại.

## Execute

### Đơn giản nhất (dùng mật khẩu được tạo)

```bash
sudo ops/rls_cutover.sh
```

Script sẽ:
- Tạo mật khẩu ngẫu nhiên 48 ký tự (hex, từ `openssl rand -hex 24`).
- Ghi vào `<MCP_ENV>` (và stdout ở cuối — **lưu lại!**).
- Chạy tất cả các bước.
- In mật khẩu cuối cùng cho operator lưu vào secrets manager (cùng FERNET_KEY).

### Dùng mật khẩu có sẵn

```bash
sudo OSM_READER_PASSWORD='your-secret-password' ops/rls_cutover.sh
```

Hữu ích khi quản lý bằng secrets manager và đã tạo `osm_reader` trước đó.

### Override placeholders (staging, fork)

```bash
sudo DB_NAME=staging_db PG_CONTAINER=pg-staging APP_USER=staging-mcp \
  MCP_SERVICE=mcp-staging MCP_ENV=/home/staging-mcp/etc/mcp.env \
  ops/rls_cutover.sh
```

## Verify (post-cutover)

Sau khi script kết thúc với exit code 0, kiểm tra thêm để yên tâm:

```bash
# 1. Service up
systemctl is-active <MCP_SERVICE>
# Output: active

# 2. Health endpoint
curl -s http://localhost:8002/health | jq .embeddings_total
# Output: <baseline count từ bước 0>

# 3. MCP connections dùng osm_reader (KHÔNG <DB_OWNER>)
docker exec <PG_CONTAINER> psql -U <DB_OWNER> -d <DB_NAME> \
  -tAc "SELECT usename FROM pg_stat_activity WHERE datname='<DB_NAME>'"
# Output: should include osm_reader (possibly multiple connections)

# 4. No MCP connections as <DB_OWNER> (if reused DSN, those connections should be gone)
# (inspect output từ lệnh trên — <DB_OWNER> entries should be 0 hoặc chỉ ops/webui)

# 5. RLS enforcement: verify policy + FORCE
docker exec <PG_CONTAINER> psql -U <DB_OWNER> -d <DB_NAME> -tAc \
  "SELECT schemaname, tablename, rowsecurity, forcerowsecurity FROM pg_tables WHERE tablename='embeddings'"
# Output: public | embeddings | t | t  (rowsecurity=true, forcerowsecurity=true)
```

## Rollback

### Automatic (script failure)

Script tự động rollback nếu xác minh fail — bạn sẽ thấy tin nhắn:
```
!! VERIFICATION FAILED — rolling back (revert MCP to owner DSN) !!
```

Script sẽ:
- Xóa `<MCP_ENV>`.
- Restart MCP (quay lại DSN chủ sở hữu từ env chính).
- Để `osm_reader` role + FORCE cờ nguyên trạng.

### Manual (operator initiate)

Nếu bạn muốn rollback sau khi cutover thành công:

```bash
# 1. Xóa DSN MCP-only
sudo rm <MCP_ENV>

# 2. Restart MCP (quay lại chủ sở hữu)
sudo systemctl restart <MCP_SERVICE>

# 3. (Optional) Revert FORCE cờ (nếu bạn muốn dọn dẹp)
docker exec <PG_CONTAINER> psql -U <DB_OWNER> -d <DB_NAME> -c \
  "ALTER TABLE embeddings NO FORCE ROW LEVEL SECURITY;"
```

Sau manual rollback, `osm_reader` role + policy vẫn còn — vô hại, chỉ không được sử dụng. Re-run cutover sau đó là an toàn.

## Maintenance window

- **Downtime**: ~30–60 giây (MCP restart + reconnect).
- **Khuyến cáo**: chạy vào **low-traffic window** (tối nửa đêm, weekend) để giảm việc client retry.
- **Health endpoint**: trả về 503 trong khoảng restart → client nên retry (timeout 60s safe).
- **Không cần** downtime các service khác (webui, indexer, backup không bị ảnh hưởng — chúng không dùng `<MCP_ENV>`).

## Troubleshooting

### Policy missing — "migration m13_004 not applied"

**Lỗi:**
```
FATAL: policy embeddings_tenant missing — run 'python -m src.db.migrate' first (m13_004)
```

**Nguyên nhân:** Migration m13_004 chưa được apply.

**Sửa:**
```bash
python -m src.db.migrate
sudo ops/rls_cutover.sh
```

### Container not found

**Lỗi:**
```
FATAL: postgres container '<PG_CONTAINER>' not found
```

**Nguyên nhân:** Tên container sai hoặc container không chạy.

**Sửa:**
```bash
# Tìm container thực tế
docker compose ps | grep postgres
# Sau đó:
sudo PG_CONTAINER=odoo-semantic-postgres-custom ops/rls_cutover.sh
```

### Cannot connect to database as owner

**Lỗi:**
```
FATAL: cannot connect to <DB_NAME> as owner <DB_OWNER>
```

**Nguyên nhân:** DSN sai, user/password sai, hoặc PostgreSQL không chạy.

**Sửa:**
```bash
# Kiểm tra kết nối thủ công
docker exec <PG_CONTAINER> psql -U <DB_OWNER> -d <DB_NAME> -c "SELECT 1"
# Nếu fail, xem log: docker logs <PG_CONTAINER>
```

### Verification failed — embeddings_total mismatch

**Lỗi:**
```
WARN: /health total != baseline — GAP1 wrap or DSN issue
!! VERIFICATION FAILED — rolling back ...
```

**Nguyên nhân**: 
- MCP kết nối thành công nhưng RLS scope bị sai (GUC không được đặt).
- Hoặc DSN không thực sự chuyển sang `osm_reader` (mcp.env không được tải).
- Hoặc có rows mới được thêm vào trong cutover (~1 giây) — unlikely nhưng có thể.

**Sửa:**
- Kiểm tra `<MCP_ENV>` tồn tại + readable by `<APP_USER>`: `sudo -u <APP_USER> cat <MCP_ENV>`.
- Kiểm tra MCP log: `journalctl -u <MCP_SERVICE> -n 50`.
- Xác minh RLS policy: `docker exec <PG_CONTAINER> psql -U <DB_OWNER> -d <DB_NAME> -c "SELECT pg_get_policy_def('embeddings_tenant')"`.
- Sửa vấn đề, sau đó chạy lại cutover script.

### Cross-tenant smoke test failed

**Lỗi:**
```
-- cross-tenant as osm_reader (GUC=profile_a): rows of profile_b expect 0, own expect >0 --
cross_tenant(expect 0)=5  ← WRONG! Expected 0.
```

**Nguyên nhân**: RLS policy không được áp dụng đúng. Có thể:
- GUC `app.allowed_profiles` không được đặt bởi ứng dụng (scope thủ công qua psql không theo cùng cách).
- Policy logic bị sai.

**Sửa:**
- **Không phải** lỗi của script — đây là bug ứng dụng (server.py không đặt GUC).
- Kiểm tra `src/mcp/server.py:_rls_read_tx()` đặt GUC trước khi truy vấn.
- Cuối cùng, kiểm tra xem bảng `embeddings` có cột `profile_name` không: `\d embeddings` trong psql.
- Fix vấn đề ứng dụng, sau đó chạy lại cutover.

### Fresh install — cross-tenant smoke skipped

**Lỗi:**
```
-- cross-tenant smoke skipped: <2 profiles with data (fresh install) --
```

**Nguyên nhân**: Cài đặt mới, chưa có dữ liệu multi-profile.

**Kết quả**: **PASS** — smoke test không áp dụng, không fail. Cutover thành công bình thường.

## References

- **Canonical script**: `ops/rls_cutover.sh` — thực hiện 5 bước + verify + auto-rollback.
- **SQL role + grants**: `ops/rls_create_osm_reader.sql` — tạo `osm_reader` role, cấp quyền granular.
- **Policy + ENABLE**: `migrations/m13_004_embeddings_rls.sql` — arm policy (armed-but-dormant), GUC setup.
- **Architecture decision**: [ADR-0034](../adr/0034-multi-tenant-pooled-isolation.md) — multi-tenant pooled isolation (D6 RLS, A5 cutover).
- **Production layout**: ADR-0027 — canonical defaults (phần đặt tên container, path, user).
