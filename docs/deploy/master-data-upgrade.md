# Master Data Upgrade Runbook

> **Audience**: admin nâng cấp production deployment hiện hữu của `odoo-semantic-mcp` để
> nhận master data mặc định (26 profiles + 48 repos seeded tự động).
> Xem [`docs/deploy.md`](../deploy.md) cho fresh install.

## Mục Tiêu

`python -m src.db.migrate` từ version này trở đi tự động seed 26 master data
profiles + 48 repos rows vào PostgreSQL:

- **Odoo CE v8 → v19** (12 profiles, mỗi profile 1 repo `Viindoo/odoo`).
- **Standard Viindoo v8 → v19** (12 profiles, 1–3 repos *delta* — chỉ addons,
  không gồm Odoo CE base; xem [Mô Hình Profile Delta](#mô-hình-profile-delta)).
- **Viindoo Internal v17 và v18** (2 profiles, 3–4 repos *delta* — chỉ internal repos).

Seeding **idempotent** qua `ON CONFLICT DO NOTHING` — chạy nhiều lần an toàn,
và **không phá** profiles bạn đã tạo thủ công trước đó. `clone_status` mặc định
là `manual` — admin chủ động bấm Clone trong Web UI khi sẵn sàng (tiết kiệm
disk + bandwidth khi chưa cần dùng hết).

## Mô Hình Profile Delta

PostgreSQL schema có `UNIQUE (url, branch)` trên bảng `repos` — một (url, branch)
chỉ có thể thuộc về **một** profile duy nhất. Master data vì vậy tổ chức
**delta-only**:

| Tier | Phần được sở hữu |
|------|------------------|
| `odoo_N` | Chỉ `Viindoo/odoo @ N.0` (Odoo CE base) |
| `standard_viindoo_N` | Chỉ Viindoo addons (`tvtmaaddons`, `erponline-enterprise` v10+, `branding` v13+) |
| `viindoo_internal_N` | Chỉ internal repos (`saas-infrastructure`, `saas-infrastructure-common`, `themes` v17-only, `odoo-api`) |

Để dùng **Standard Viindoo** cho 1 version, admin index CẢ `odoo_N` VÀ
`standard_viindoo_N`. Để dùng **Viindoo Internal**, index thêm
`viindoo_internal_N`. MCP queries (`resolve_model`, `find_examples`, …) tự
combine vì chúng scope theo `odoo_version`, không phải `profile_name` —
nghĩa là `Viindoo/odoo @ 17.0` (thuộc `odoo_17`) và `tvtmaaddons @ 17.0`
(thuộc `standard_viindoo_17`) đều xuất hiện chung khi query version `17.0`.

## Trước Khi Nâng Cấp

Checklist:

1. **Stop services** nếu policy yêu cầu zero-write trong upgrade window:
   ```bash
   sudo systemctl stop odoo-semantic-mcp odoo-semantic-webui
   ```
   (Migrate được bảo vệ bởi `pg_advisory_lock` `0x05DA0E05` nên có thể chạy
   không cần stop, nhưng best practice cho production critical workflow là stop
   trước.)

2. **Backup PostgreSQL** (BẮT BUỘC):
   ```bash
   pg_dump $PG_DSN > backup_pre_master_data_$(date +%F).sql
   ```
   File này là điểm rollback duy nhất nếu cần đảo ngược seeding.

3. **Disk space check** — backup `.sql` thường 50 MB–500 MB tuỳ data volume.
   `df -h /var/lib` để confirm.

4. **Verify venv tồn tại** `~/.venv/odoo-semantic-mcp` (hoặc đường dẫn deployment
   của bạn). `~/.venv/odoo-semantic-mcp/bin/python --version` phải ≥ 3.12.

## Bước Nâng Cấp

```bash
# 1. Pull code mới (chứa migrations/0002_master_data_seed.sql + src/db/seed_master_data.py)
cd /home/odoo-semantic/odoo-semantic-mcp
sudo -u odoo-semantic git pull

# 2. Run migrate via service user
#    QUAN TRỌNG: service-user vì repos.local_path được derive từ Path.home();
#    chạy bằng root/user khác sẽ ghi sai path → cloner khi trigger sẽ clone nhầm chỗ.
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    ~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
'
```

Output mong đợi:

```
✓ Migrations applied to postgresql://...
✓ Seeded master data: X profiles new, Y unchanged; P repos new, Q unchanged
```

Với deployment đã có manual profiles trùng tên seed: `Y > 0` và `Q > 0` — đó
là dấu hiệu admin data win, đúng intent.

## Verify

### 1. List profiles via CLI

```bash
sudo -u odoo-semantic ~/.venv/odoo-semantic-mcp/bin/python -m src.manager list
```

Kỳ vọng: thấy 26 seeded profiles `odoo_8` ... `odoo_19`, `standard_viindoo_8` ...
`standard_viindoo_19`, `viindoo_internal_17`, `viindoo_internal_18` — kèm
profiles thủ công cũ (nếu có).

### 2. SQL spot-check số lượng

```sql
SELECT name, odoo_version FROM profiles
WHERE name LIKE 'odoo\_%'
   OR name LIKE 'standard\_viindoo\_%'
   OR name LIKE 'viindoo\_internal\_%'
ORDER BY name;
-- Expected: 26 rows
```

### 3. Repo count per profile

```sql
SELECT p.name, COUNT(r.id) AS repo_count
FROM profiles p
LEFT JOIN repos r ON r.profile_id = p.id
WHERE p.name LIKE 'odoo\_%'
   OR p.name LIKE 'standard\_viindoo\_%'
   OR p.name LIKE 'viindoo\_internal\_%'
GROUP BY p.name ORDER BY p.name;
```

Expected:

| Profile tier | Repo count (delta) |
|---|---|
| `odoo_8` … `odoo_19` | **1** each (`Viindoo/odoo`) |
| `standard_viindoo_8`, `standard_viindoo_9` | **1** each (`tvtmaaddons`) |
| `standard_viindoo_10` … `standard_viindoo_12` | **2** each (+`erponline-enterprise`) |
| `standard_viindoo_13` … `standard_viindoo_19` | **3** each (+`branding`) |
| `viindoo_internal_17` | **4** (`saas-infrastructure`, `saas-infrastructure-common`, `themes`, `odoo-api`) |
| `viindoo_internal_18` | **3** (no `themes` — max branch là 17.0) |

### 4. `clone_status` chỉ là `manual`

```sql
SELECT DISTINCT clone_status FROM repos
WHERE profile_id IN (
    SELECT id FROM profiles
    WHERE name LIKE 'odoo\_%' OR name LIKE 'standard\_viindoo\_%' OR name LIKE 'viindoo\_internal\_%'
);
-- Expected: chỉ 1 row 'manual'
```

## Edge Cases & Xử Lý

| Tình huống | Hành vi | Xử lý |
|---|---|---|
| Profile name đã có (vd `viindoo_internal_17` manual) | Seed skip (`ON CONFLICT (name) DO NOTHING`) | Admin data win. Review trùng lặp qua `python -m src.manager list`. |
| Repo URL+branch đã có dưới profile khác | Seed skip (UNIQUE constraint) | OK — không double-add. Admin có thể move repo qua DB nếu muốn liên kết với seeded profile. |
| `Path.home()` mismatch (chạy bằng root thay vì service-user) | `repos.local_path` trỏ sai chỗ | Chạy migrate qua `sudo -u <service-user>` như runbook. Nếu lỡ chạy sai, có thể reset qua section Rollback. |
| Code update thêm version mới (vd v20) | Migration `0002` đã marked applied — yoyo skip | Chạy `python -m src.manager seed-master-data` để pick up version mới (CLI idempotent UPSERT). |

## Re-Seed Sau Code Update

Khi pull code có thêm version mới hoặc thêm repo cho version sẵn có,
**migration SQL không chạy lại** (yoyo state). Dùng CLI:

```bash
sudo -u odoo-semantic -H bash -c '
    export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
    ~/.venv/odoo-semantic-mcp/bin/python -m src.manager seed-master-data
'
```

CLI gọi cùng `seed_all()` function — INSERT mới + skip existing. Output báo
cáo (`N new, M unchanged`) rõ ràng để admin biết thay đổi.

Flag tùy chọn:
- `--profiles-only` — chỉ seed `profiles` table, bỏ qua `repos`.
- `--reset` — **DESTRUCTIVE**, xem section Rollback.

## Rollback / Disaster Recovery

### A. Restore PostgreSQL backup (khôi phục toàn bộ)

```bash
sudo -u postgres psql odoo_semantic_db < backup_pre_master_data_$(date +%F).sql
```

Đây là cách an toàn nhất — đảo ngược hoàn toàn về trạng thái trước migrate.

### B. Destructive reset (chỉ xóa seeded data)

```bash
sudo -u odoo-semantic ~/.venv/odoo-semantic-mcp/bin/python -m src.manager seed-master-data --reset
```

CLI sẽ prompt nhập chuỗi `YES` để confirm. Sau confirm: DELETE mọi profile có
tên match `odoo\_%`, `standard\_viindoo\_%`, hoặc `viindoo\_internal\_%`.
`ON DELETE CASCADE` xóa luôn child repos. Sau đó seeded lại từ đầu.

**Cảnh báo**: `--reset` cũng xóa profile manual nếu admin lỡ đặt tên trùng
prefix seed. Backup PG TRƯỚC khi dùng `--reset`.

---

Quay về [`docs/deploy.md`](../deploy.md) cho phần còn lại của deployment guide.
