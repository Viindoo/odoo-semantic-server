# Backup Runbook — Odoo Semantic MCP

Hướng dẫn vận hành backup tự động và thủ công cho OSM.

> **Bilingual note:** English headers; Vietnamese subnotes per project style.

---

## Install Systemd Timer

Cài timer để backup chạy tự động mỗi đêm lúc 03:00.

```bash
# Copy unit files
sudo cp docs/deploy/odoo-semantic-backup.service /etc/systemd/system/
sudo cp docs/deploy/odoo-semantic-backup.timer   /etc/systemd/system/

# Reload và enable timer (service chạy on-demand qua timer — không enable service trực tiếp)
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-backup.timer

# Verify
sudo systemctl status odoo-semantic-backup.timer
systemctl list-timers odoo-semantic-backup.timer
```

---

## Logrotate

Logrotate config đã có tại `docs/deploy/logrotate.d/odoo-semantic`. Cài:

```bash
sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/odoo-semantic
# Test config
sudo logrotate --debug /etc/logrotate.d/odoo-semantic
```

---

## Environment Requirements

Backup CLI cần `PG_DSN` để kết nối PostgreSQL. Có 2 cách cung cấp (theo thứ tự ưu tiên):

### 1. Env var trong `.env` (ưu tiên cao hơn)

```bash
# /opt/odoo-semantic-mcp/.env
PG_DSN=postgresql://odoo_semantic:password@localhost:5432/odoo_semantic
```

File này được load bởi `EnvironmentFile=` trong `odoo-semantic-backup.service`.

### 2. INI config (fallback)

```ini
# ~/.odoo-semantic/odoo-semantic.conf  OR  ./odoo-semantic.conf
[database]
pg_dsn = postgresql://odoo_semantic:password@localhost:5432/odoo_semantic
```

---

## Manual Backup Run

Chạy backup ngay lập tức (không cần đợi timer):

```bash
# Qua systemd (recommended — dùng đúng env + user)
sudo systemctl start odoo-semantic-backup.service

# Kiểm tra kết quả
sudo journalctl -u odoo-semantic-backup.service -n 50

# Hoặc chạy trực tiếp (debug)
mkdir -p /var/backups/odoo-semantic
~/.venv/odoo-semantic-mcp/bin/python -m src.cli backup \
    --output /var/backups/odoo-semantic/osm-manual-$(date +%Y%m%d-%H%M%S).tar.gz
```

---

## Restore from Bundle

Tham khảo [`docs/deploy/disaster-recovery.md`](disaster-recovery.md) cho full restore runbook.

Restore nhanh từ bundle:

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.cli restore \
    /var/backups/odoo-semantic/osm-20260517-030001.tar.gz
```

> **Lưu ý:** Restore tự động tạo pre-restore safety backup trước khi ghi đè data.

---

## Troubleshooting

### "PG_DSN not configured"

**Root cause:** Biến `PG_DSN` không có trong env và không có trong INI config.

**Fix:**

```bash
# Option A: thêm vào .env
echo 'PG_DSN=postgresql://odoo_semantic:password@localhost:5432/odoo_semantic' \
    >> /opt/odoo-semantic-mcp/.env

# Option B: thêm vào INI config
mkdir -p ~/.odoo-semantic
cat >> ~/.odoo-semantic/odoo-semantic.conf << 'EOF'
[database]
pg_dsn = postgresql://odoo_semantic:password@localhost:5432/odoo_semantic
EOF
```

Sau đó kiểm tra: `grep PG_DSN /opt/odoo-semantic-mcp/.env`

### "pg_dump not found" / "psql not found"

**Root cause trước đây:** Host không cài Postgres client tools; Postgres chỉ chạy trong Docker container.

**Hiện tại đã được xử lý tự động:** Hàm `_resolve_postgres_tool()` trong `src/cli.py` tự detect:
- Nếu `pg_dump`/`psql` có trên PATH → dùng trực tiếp
- Nếu không có → fallback sang `docker exec -i odoo-semantic-mcp-postgres-1 pg_dump` (transparent)

Nếu Docker container tên khác mặc định (`odoo-semantic-mcp-postgres-1`), set env var:

```bash
export POSTGRES_CONTAINER=my-custom-postgres-container-name
```

Hoặc thêm vào `.env`:

```bash
POSTGRES_CONTAINER=my-custom-postgres-container-name
```

### Timer không chạy

```bash
# Kiểm tra timer status
systemctl status odoo-semantic-backup.timer

# Xem log gần nhất
sudo journalctl -u odoo-semantic-backup.service --since "24 hours ago"

# Force chạy ngay
sudo systemctl start odoo-semantic-backup.service
```
