# Pre-Launch Checklist — Odoo Semantic MCP

Danh sách kiểm tra trước khi mở public / phân phát API key cho team.  
Admin phải ký tên vào mọi mục bên dưới (ghi `[x]` + ngày + ghi chú nếu cần).

> **Bilingual note:** English headers; Vietnamese subnotes per project style.

---

## 1. Infrastructure & TLS

**Verify HTTPS + HSTS active.**

- [ ] `curl -I https://<domain>/health` → HTTP 200, header `Strict-Transport-Security` có mặt
  - *Nếu thiếu HSTS header: thêm `add_header Strict-Transport-Security` vào nginx server block (xem docs/deploy.md §4.1)*
- [ ] Certbot timer chạy OK: `systemctl status certbot.timer` → `active (waiting)` hoặc `active (running)`
  - *Caddy auto-renew: `sudo caddy reload` sau khi domain verified*
- [ ] Port 443 TLS hoạt động (nếu dùng variant): `curl -I https://<domain>/health` → 200
  - *Port 443 dành cho install page public; xem nginx.conf.example §Port 443 variant*

---

## 2. Auth & Rate Limiting

**Xác nhận API key auth bắt buộc và rate limit cấu hình.**

- [ ] `curl https://<domain>/mcp` không có X-API-Key → **HTTP 401** (không bypass được)
  - *Nếu trả 200: kiểm tra AuthMiddleware mount trong src/mcp/server.py*
- [ ] `curl https://<domain>/health` → HTTP 200 (không cần key — load balancer health check)
  - *Health endpoint bypass auth theo thiết kế — đây là đúng*
- [ ] `rate_limit_rpm = 120` (hoặc giá trị phù hợp) trong `odoo-semantic.conf [auth]`
  - *Kiểm tra: `sudo grep rate_limit_rpm /etc/odoo-semantic/odoo-semantic.conf`*
- [ ] Ít nhất 1 API key đã tạo: `python -m src.manager list` → thấy key name
  - *Tạo key: `python -m src.manager create-api-key admin`*

---

## 3. Port Isolation

**Web UI port 8003 không reachable từ external host.**

- [ ] Từ external host: `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` → connection refused hoặc timeout
  - *Nếu reach được: kiểm tra `odoo-semantic.conf [server]` + firewall rules — 8003 phải bind 127.0.0.1 only*
- [ ] DB ports không expose: `sudo ss -tlnp | grep -E '7687|5432'` → chỉ bind `127.0.0.1` (không `0.0.0.0`)
  - *Nếu bind 0.0.0.0: sửa docker-compose.yml → `"127.0.0.1:7687:7687"`*
- [ ] Docker daemon không expose TCP: `sudo ss -tlnp | grep 2375` → trống (Unix socket only)

---

## 4. Logrotate

**Log file reindex không phình to theo thời gian.**

- [ ] `/etc/logrotate.d/odoo-semantic` tồn tại: `ls /etc/logrotate.d/odoo-semantic`
  - *Cài: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/`*
- [ ] Dry-run sạch: `sudo logrotate --debug /etc/logrotate.d/odoo-semantic` → không có error

---

## 5. Backup & Recovery

**Backup có thể restore thành công (không chỉ tạo file).**

- [ ] Backup PG chạy được: `python -m src.cli backup --output /tmp/test-backup.sql` → file > 0 bytes
- [ ] Backup Neo4j chạy được: neo4j dump command (xem docs/deploy.md §2.4) → file `~/backups/neo4j-<DATE>.dump` tạo thành công
- [ ] Restore thử trên non-production: restore PG + count `SELECT COUNT(*) FROM profiles` > 0
  - *Tham khảo docs/deploy/disaster-recovery.md — bắt buộc test ít nhất 1 lần trước launch*
- [ ] `webui.env` (FERNET_KEY) backed up vào secrets manager riêng biệt — **không chỉ trên server disk**
  - *Mất FERNET_KEY = không decrypt SSH private key trong DB*

---

## 6. MCP Tool Sign-Off (All 14 Tools)

**Mỗi tool phải trả về kết quả có cấu trúc — không được empty hoặc error.**

Chạy từ Claude Code với key `osm_xxxx...` đã cấu hình:

| # | Tool | Lệnh gọi ví dụ | Expected signal | Sign-off |
|---|------|----------------|-----------------|----------|
| 1 | `resolve_model` | `resolve_model("account.move", "17.0")` | Header `account.move (Odoo 17.0)` + `Inheritance` ≥ 1 module + `Fields` non-empty | `[ ]` |
| 2 | `resolve_field` | `resolve_field("amount_total", "account.move", "17.0")` | Type, computed/related info, extension chain | `[ ]` |
| 3 | `resolve_method` | `resolve_method("action_post", "account.move", "17.0")` | Override chain + super() calls | `[ ]` |
| 4 | `resolve_view` | `resolve_view("sale.view_order_form", "17.0")` | View chain ≥ 1 entry + XPath list (có thể empty) | `[ ]` |
| 5 | `find_examples` | `find_examples("compute tax based on partner country")` | 5 results với file path + score *(skip nếu `--no-embed`)* | `[ ]` |
| 6 | `impact_analysis` | `impact_analysis("field", "sale.order.amount_total", "17.0")` | `Risk: <LOW\|MEDIUM\|HIGH>` + Views + JS patches sections | `[ ]` |
| 7 | `lookup_core_api` | `lookup_core_api("name_get", "17.0")` | Status (active/deprecated/removed) + description | `[ ]` |
| 8 | `api_version_diff` | `api_version_diff("name_get", "16.0", "17.0")` | Diff giữa 2 version — thay đổi signature hoặc status | `[ ]` |
| 9 | `find_deprecated_usage` | `find_deprecated_usage("17.0")` | List deprecated API usages trong code (có thể empty nếu code clean) | `[ ]` |
| 10 | `lint_check` | `lint_check("sale", "17.0")` | Lint rule hits list hoặc "no violations" | `[ ]` |
| 11 | `cli_help` | `cli_help("server", "--gevent-port", "17.0")` | Flag description + version added/removed | `[ ]` |
| 12 | `suggest_pattern` | `suggest_pattern("computed field cross-model partner_id")` | 3-5 PatternExample với code snippet + gotchas | `[ ]` |
| 13 | `check_module_exists` | `check_module_exists("knowledge", "17.0")` | is_ee_confusion flag + EE warning nếu applicable | `[ ]` |
| 14 | `find_override_point` | `find_override_point("sale.order", "action_confirm", "17.0")` | super_safety + super_ratio + anti-patterns | `[ ]` |

> *Tools 7–11 cần `index-core` đã chạy. Tool 12–14 cần `seed_patterns` đã chạy. Tool 5 cần Ollama + re-index không `--no-embed`.*

### Persona Skills (M7.5)

Verify cross-vendor adapter files are accessible and persona skills are documented. These do not require server-side verification — check that files exist and links in README resolve.

| Skill | Persona | Tools Used | Sign-off |
|-------|---------|------------|---------|
| `odoo-risk-overview` | CEO | `impact_analysis`, `find_deprecated_usage`, `check_module_exists` | `[ ]` |
| `odoo-customization-inventory` | CEO | `resolve_model`, `check_module_exists` | `[ ]` |
| `odoo-override-finder` | Developer | `find_override_point`, `resolve_method`, `suggest_pattern` | `[ ]` |
| `odoo-deprecation-audit` | Developer | `find_deprecated_usage`, `api_version_diff`, `lookup_core_api` | `[ ]` |
| `odoo-version-diff` | Developer/Marketer | `api_version_diff`, `lookup_core_api` | `[ ]` |
| `odoo-feature-check` | Consultant | `check_module_exists`, `resolve_model`, `find_examples` | `[ ]` |
| `odoo-gap-analysis` | Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` | `[ ]` |
| `odoo-feature-highlights` | Marketer | `api_version_diff`, `find_examples`, `resolve_model` | `[ ]` |
| `odoo-addon-diff` | Marketer | `check_module_exists`, `resolve_model` | `[ ]` |
| `odoo-capability-proof` | Sales | `find_examples`, `check_module_exists`, `resolve_model` | `[ ]` |
| `odoo-objection-handler` | Sales | `check_module_exists`, `find_examples`, `suggest_pattern` | `[ ]` |

> *Persona skill verification: confirm `dist/gemini-gem-instructions.md`, `dist/openai-gpt-instructions.md`, `dist/cursor-rules.md`, and `docs/personas/*.md` are present in the deployed repo. Spot-check one skill per persona using the sample questions in `docs/personas/`.*

---

## 7. Install Page

**Trang `/install/` hoạt động và hiển thị snippet đúng cho các AI tool.**

- [ ] `https://<domain>/install/` → load thành công, không 404
- [ ] Dán API key vào form → snippet cho Claude Code hiển thị đúng URL + header
  - *Snippet phải chứa đúng domain + port, không phải localhost*

---

## 8. Systemd Services

**Services tự-restart khi crash, và sẽ khởi động lại sau reboot.**

- [ ] `systemctl is-enabled odoo-semantic-mcp` → `enabled`
- [ ] `systemctl is-enabled odoo-semantic-webui` → `enabled`
- [ ] Simulate crash: `sudo systemctl kill -s SIGKILL odoo-semantic-mcp` → sau 5s `systemctl status` → `active (running)` lại
  - *Restart policy: `Restart=on-failure` trong service file*

---

## 9. Indexer Cron

**Cron job chạy và log ghi được.**

- [ ] `/etc/cron.d/odoo-semantic-reindex` tồn tại: `ls /etc/cron.d/odoo-semantic-reindex`
- [ ] Chạy thủ công 1 lần để verify: `sudo -u odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all` → exit 0
- [ ] Log ghi được: `tail /var/log/odoo-semantic-reindex.log` → có output

---

## 10. Web UI Session Auth (M7 W16)

**Xác nhận session-based auth hoạt động đúng trước khi mở Web UI (port 8003).**

- [ ] `create-webui-user` đã chạy — ít nhất 1 admin user tồn tại:
  `python -m src.manager list-webui-users` → thấy ít nhất 1 user
  - *Tạo user: `python -m src.manager create-webui-user admin` (prompt mật khẩu)*
- [ ] Unauthenticated GET `/repos` → 302 redirect đến `/login`:
  `curl -I http://127.0.0.1:8003/repos` → `Location: /login`
- [ ] POST `/login` với sai mật khẩu → flash error (không grant session):
  `curl -c /tmp/test.jar -b /tmp/test.jar -d 'username=admin&password=WRONG&next=/' http://127.0.0.1:8003/login -L -s | grep error`
  → phải thấy error indicator trong redirect URL
- [ ] GET `/logout` clears session → tiếp theo request tới `/` → 302 `/login`
- [ ] `WEBUI_SESSION_SECRET` đã set trong `webui.env` (không dùng auto-generated ephemeral secret):
  `sudo grep WEBUI_SESSION_SECRET /etc/odoo-semantic/webui.env` → non-empty value

---

## 11. Pre-Launch Sign-Off

Admin điền vào bảng sau trước khi phân phát API key cho team:

| Mục | Admin | Ngày | Ghi chú |
|-----|-------|------|---------|
| Infrastructure & TLS (§1) | | | |
| Auth & Rate Limiting (§2) | | | |
| Port Isolation (§3) | | | |
| Logrotate (§4) | | | |
| Backup & Recovery (§5) | | | |
| MCP Tool Sign-Off tất cả 14 tools (§6) | | | |
| Install Page (§7) | | | |
| Systemd Services (§8) | | | |
| Indexer Cron (§9) | | | |
| Web UI Session Auth (§10) | | | |

**Khi tất cả 11 mục `[x]` → deploy ready. Phân phát key + URL.**

---

*Xem thêm: [docs/deploy.md](../deploy.md) · [docs/deploy/disaster-recovery.md](disaster-recovery.md)*
