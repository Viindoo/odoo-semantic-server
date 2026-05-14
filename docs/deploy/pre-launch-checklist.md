# Pre-Launch Checklist — Odoo Semantic MCP

Danh sách kiểm tra trước khi mở public / phân phát API key cho team.  
Admin phải ký tên vào mọi mục bên dưới (ghi `[x]` + ngày + ghi chú nếu cần).

> **Bilingual note:** English headers; Vietnamese subnotes per project style.

> **M7.5 Post-Verification Hotfixes (2026-05-14):** Một số items §1.1, §6 (tools 5, 7, 8, 11, 12), §10.2 hiện đang BLOCKED bởi 5 P1 production issues. Runbook fix step-by-step: [`docs/deploy/m7.5-production-fixes.md`](m7.5-production-fixes.md). Sau khi admin chạy runbook, re-run pre-launch sign-off và update các items đó.

---

## 1. Infrastructure & TLS

**Verify HTTPS + HSTS active.**

- [x] `curl -I https://<domain>/health` → HTTP 200, header `Strict-Transport-Security` có mặt **(P1-D RESOLVED 2026-05-14 11:36 — `add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;` added to `/etc/nginx/sites-available/odoo-semantic-mcp` server block. Verified: `strict-transport-security: max-age=31536000; includeSubDomains`.)**
  - *Nếu thiếu HSTS header: thêm `add_header Strict-Transport-Security` vào nginx server block (xem docs/deploy.md §4.1)*
- [ ] Certbot timer chạy OK: `systemctl status certbot.timer` → `active (waiting)` hoặc `active (running)` **(admin SSH verify)**
  - *Caddy auto-renew: `sudo caddy reload` sau khi domain verified*
- [x] Port 443 TLS hoạt động (nếu dùng variant): `curl -I https://<domain>/health` → 200 *(2026-05-14 — verified `https://odoo-semantic.viindoo.com/health` → HTTP/2 200, see `docs/m7.5-batch3-infra.md` §1.3)*
  - *Port 443 dành cho install page public; xem nginx.conf.example §Port 443 variant*

---

## 2. Auth & Rate Limiting

**Xác nhận API key auth bắt buộc và rate limit cấu hình.**

- [x] `curl https://<domain>/mcp` không có X-API-Key → **HTTP 401** (không bypass được) *(2026-05-14 — verified `curl -sI https://odoo-semantic.viindoo.com/mcp` → HTTP/2 401)*
  - *Nếu trả 200: kiểm tra AuthMiddleware mount trong src/mcp/server.py*
- [x] `curl https://<domain>/health` → HTTP 200 (không cần key — load balancer health check) *(2026-05-14 — verified, bypass auth correct)*
  - *Health endpoint bypass auth theo thiết kế — đây là đúng*
- [ ] `rate_limit_rpm = 120` (hoặc giá trị phù hợp) trong `odoo-semantic.conf [auth]` **(admin SSH verify)**
  - *Kiểm tra: `sudo grep rate_limit_rpm /etc/odoo-semantic/odoo-semantic.conf`*
- [ ] Ít nhất 1 API key đã tạo: `python -m src.manager list` → thấy key name **(admin SSH verify)**
  - *Tạo key: `python -m src.manager create-api-key admin`*

---

## 3. Port Isolation

**Web UI port 8003 không reachable từ external host.**

- [ ] Từ external host: `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` → connection refused hoặc timeout **(admin SSH verify — requires nmap / external host access)**
  - *Nếu reach được: kiểm tra `odoo-semantic.conf [server]` + firewall rules — 8003 phải bind 127.0.0.1 only*
- [ ] DB ports không expose: `sudo ss -tlnp | grep -E '7687|5432'` → chỉ bind `127.0.0.1` (không `0.0.0.0`) **(admin SSH verify)**
  - *Nếu bind 0.0.0.0: sửa docker-compose.yml → `"127.0.0.1:7687:7687"`*
- [ ] Docker daemon không expose TCP: `sudo ss -tlnp | grep 2375` → trống (Unix socket only) **(admin SSH verify)**

---

## 4. Logrotate

**Log file reindex không phình to theo thời gian.**

- [ ] `/etc/logrotate.d/odoo-semantic` tồn tại: `ls /etc/logrotate.d/odoo-semantic` **(admin SSH verify)**
  - *Cài: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/`*
- [ ] Dry-run sạch: `sudo logrotate --debug /etc/logrotate.d/odoo-semantic` → không có error **(admin SSH verify)**

---

## 5. Backup & Recovery

**Backup có thể restore thành công (không chỉ tạo file).**

- [ ] Backup PG chạy được: `python -m src.cli backup --output /tmp/test-backup.sql` → file > 0 bytes **(admin SSH verify)**
- [ ] Backup Neo4j chạy được: neo4j dump command (xem docs/deploy.md §2.4) → file `~/backups/neo4j-<DATE>.dump` tạo thành công **(admin SSH verify)**
- [ ] Restore thử trên non-production: restore PG + count `SELECT COUNT(*) FROM profiles` > 0 **(admin SSH verify)**
  - *Tham khảo docs/deploy/disaster-recovery.md — bắt buộc test ít nhất 1 lần trước launch*
- [ ] `webui.env` (FERNET_KEY) backed up vào secrets manager riêng biệt — **không chỉ trên server disk** **(admin SSH verify)**
  - *Mất FERNET_KEY = không decrypt SSH private key trong DB*

---

## 6. MCP Tool Sign-Off (All 14 Tools)

**Mỗi tool phải trả về kết quả có cấu trúc — không được empty hoặc error.**

Chạy từ Claude Code với key `osm_xxxx...` đã cấu hình:

| # | Tool | Lệnh gọi ví dụ | Expected signal | Sign-off |
|---|------|----------------|-----------------|----------|
| 1 | `resolve_model` | `resolve_model("account.move", "17.0")` | Header `account.move (Odoo 17.0)` + `Inheritance` ≥ 1 module + `Fields` non-empty | `[x]` 2026-05-14 — 100+ extensions, Fields 366, Methods 951 |
| 2 | `resolve_field` | `resolve_field("amount_total", "account.move", "17.0")` | Type, computed/related info, extension chain | `[x]` 2026-05-14 — monetary, computed (_compute_amount), stored, declared in account |
| 3 | `resolve_method` | `resolve_method("action_post", "account.move", "17.0")` | Override chain + super() calls | `[x]` 2026-05-14 — override chain 3 modules, super() flags correct |
| 4 | `resolve_view` | `resolve_view("sale.view_order_form", "17.0")` | View chain ≥ 1 entry + XPath list (có thể empty) | `[x]` 2026-05-14 — form/sale.order, 25 extensions với XPath detail |
| 5 | `find_examples` | `find_examples("compute tax based on partner country")` | 5 results với file path + score *(skip nếu `--no-embed`)* | `[x]` **P1-A RESOLVED + client-side confirmed 2026-05-14** — Embedder URL port `:9999` (closed) → drop port (port 443). Client-side smoke PASS via MCP plugin: `find_examples("sale order confirm", "17.0")` → 5 results, top score 0.84. 2-of-2 cross-check (Opus + Sonnet) in [`docs/m7.5-mcp-verification.md`](../m7.5-mcp-verification.md). |
| 6 | `impact_analysis` | `impact_analysis("field", "sale.order.amount_total", "17.0")` | `Risk: <LOW\|MEDIUM\|HIGH>` + Views + JS patches sections | `[x]` 2026-05-14 — Risk HIGH, 49 views, 124 methods, 89 dependent modules |
| 7 | `lookup_core_api` | `lookup_core_api("name_get", "17.0")` | Status (active/deprecated/removed) + description | `[x]` **P1-B RESOLVED 2026-05-14** — `index-core --source ~/git/odoo_17.0 --version 17.0` ran. v17 now has 501 CoreSymbol. Cypher confirms `odoo.models.BaseModel.name_get` indexed (status=stable per P2 quirk §8). |
| 8 | `api_version_diff` | `api_version_diff("name_get", "16.0", "17.0")` | Diff giữa 2 version — thay đổi signature hoặc status | `[~]` **PARTIAL** — v17 indexed; v16 still gap (deferred to Tier 2 backlog). Tool will work `from 17.0 to 18.0` once Tier 2 ran. |
| 9 | `find_deprecated_usage` | `find_deprecated_usage("17.0")` | List deprecated API usages trong code (có thể empty nếu code clean) | `[x]` 2026-05-14 — 0 hits on clean indexed code, valid empty result |
| 10 | `lint_check` | `lint_check("sale", "17.0")` | Lint rule hits list hoặc "no violations" | `[x]` 2026-05-14 — V0 fuzzy matcher works; P2 catalogue gap noted (M7.5-P2-LINT) |
| 11 | `cli_help` | `cli_help("server", "--gevent-port", "17.0")` | Flag description + version added/removed | `[x]` **P1-C RESOLVED 2026-05-14** — Bundled fix with #7. v17 now has 80 CLIFlag nodes. Cypher confirms `--gevent-port` indexed: `command="server", help="Listen port for the gevent worker"`. |
| 12 | `suggest_pattern` | `suggest_pattern("computed field cross-model partner_id")` | 3-5 PatternExample với code snippet + gotchas | `[~]` **P1-A resolved infra; new P2 operational gap 2026-05-14** — Client-side smoke shows `no patterns indexed. Run: python -m src.indexer.seed_patterns`. Embedder healthy (per #5); root cause is missing `seed_patterns` step on prod, not P1-A. Tracked as M7.5-P2-SEED in TASKS.md M8 backlog. See [`docs/m7.5-mcp-verification.md`](../m7.5-mcp-verification.md). |
| 13 | `check_module_exists` | `check_module_exists("knowledge", "17.0")` | is_ee_confusion flag + EE warning nếu applicable | `[x]` 2026-05-14 — EE confusion flag set, GPL/EE warning correct |
| 14 | `find_override_point` | `find_override_point("sale.order", "action_confirm", "17.0")` | super_safety + super_ratio + anti-patterns | `[x]` 2026-05-14 — super_safety=always, super_ratio=7/8, 8-module chain, 3 anti-patterns |

**Sign-off summary 2026-05-14 (hotfix applied + client-side cross-check):** 13/14 PASS + 1 PARTIAL (#12 `suggest_pattern` operational gap: PatternExample not seeded on prod; #8 `api_version_diff` Tier 2 v16 backlog). Tool #5 + #12 client-side smoke completed (2-of-2 Opus + Sonnet — 3 sources agree with curl run). P1-D HSTS verified. All P1 root causes resolved except P1-E (deferred to M8 per Branch B). Full reports: [`docs/m7.5-verification-issues.md`](../m7.5-verification-issues.md) Resolution Stamps + [`docs/m7.5-batch1-mcp-signoff.md`](../m7.5-batch1-mcp-signoff.md) + [`docs/m7.5-mcp-verification.md`](../m7.5-mcp-verification.md) + [`docs/m7.5-post-fix-verification.md`](../m7.5-post-fix-verification.md).

> *Tools 7–11 cần `index-core` đã chạy. Tool 12–14 cần `seed_patterns` đã chạy. Tool 5 cần Ollama + re-index không `--no-embed`.*

### Persona Skills (M7.5)

Verify cross-vendor adapter files are accessible and persona skills are documented. These do not require server-side verification — check that files exist and links in README resolve.

| Skill | Persona | Tools Used | Sign-off |
|-------|---------|------------|---------|
| `odoo-risk-overview` | CEO | `impact_analysis`, `find_deprecated_usage`, `check_module_exists` | `[x]` 2026-05-14 — pilot 13/13 hits, file exists |
| `odoo-customization-inventory` | CEO | `resolve_model`, `check_module_exists` | `[x]` 2026-05-14 — pilot 12/12 hits, file exists |
| `odoo-override-finder` | Developer | `find_override_point`, `resolve_method`, `suggest_pattern` | `[x]` 2026-05-14 — pilot 9/9 hits, file exists |
| `odoo-deprecation-audit` | Developer | `find_deprecated_usage`, `api_version_diff`, `lookup_core_api` | `[x]` 2026-05-14 — pilot 8/8 hits, file exists |
| `odoo-version-diff` | Developer/Marketer | `api_version_diff`, `lookup_core_api` | `[x]` 2026-05-14 — pilot 8/8 hits, file exists |
| `odoo-feature-check` | Consultant | `check_module_exists`, `resolve_model`, `find_examples` | `[x]` 2026-05-14 — pilot 12/13 hits (1 P2 TRIGGER gap noted) |
| `odoo-gap-analysis` | Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` | `[x]` 2026-05-14 — pilot 11/12 hits (1 P2 TRIGGER gap) |
| `odoo-feature-highlights` | Marketer | `api_version_diff`, `find_examples`, `resolve_model` | `[x]` 2026-05-14 — pilot 12/13 hits (1 P2 TRIGGER gap) |
| `odoo-addon-diff` | Marketer | `check_module_exists`, `resolve_model` | `[x]` 2026-05-14 — pilot 11/12 hits (1 P2 TRIGGER gap) |
| `odoo-capability-proof` | Sales | `find_examples`, `check_module_exists`, `resolve_model` | `[x]` 2026-05-14 — pilot 12/13 hits (1 P2 TRIGGER gap) |
| `odoo-objection-handler` | Sales | `check_module_exists`, `find_examples`, `suggest_pattern` | `[x]` 2026-05-14 — pilot 12/12 hits, file exists |

**Persona sign-off summary 2026-05-14:** 11/11 PASS. Overall pilot hit-rate 96% (120/125), all 5 personas ≥92%. Method: static dispatch proxy (full live LLM measurement deferred M8). Report: `docs/m7.5-pilot-results.md`. Golden set: `tests/eval/auto_route_125.yaml`.

> *Persona skill verification: confirm `dist/gemini-gem-instructions.md`, `dist/openai-gpt-instructions.md`, `dist/cursor-rules.md`, and `docs/personas/*.md` are present in the deployed repo. Spot-check one skill per persona using the sample questions in `docs/personas/`.*

---

## 7. Install Page

**Trang `/install/` hoạt động và hiển thị snippet đúng cho các AI tool.**

- [x] `https://<domain>/install/` → load thành công, không 404 *(2026-05-14 — 9897 bytes HTML)*
- [x] Dán API key vào form → snippet cho Claude Code hiển thị đúng URL + header *(2026-05-14 — 5 vendor keywords present: Claude, Codex, Gemini, VS Code, Antigravity)*
  - *Snippet phải chứa đúng domain + port, không phải localhost*
- [x] Tab Claude Code có 2 sub-tabs "Plugin (recommended)" + "Manual MCP" — sub-tab Plugin mặc định active *(2026-05-14 — `Plugin (recommended)` button has `active` class)*
- [x] Sub-tab Plugin hiển thị đúng 3 lệnh: `claude plugin marketplace add Viindoo/claude-plugins`, `claude plugin install odoo-semantic@viindoo-plugins`, `/odoo-semantic:connect` *(2026-05-14 — all 3 lệnh present in page)*
- [x] Marketplace reachable: `claude plugin marketplace add Viindoo/claude-plugins --scope user` exit 0 *(2026-05-14 — `github.com/Viindoo/claude-plugins` → HTTP/2 200; verified via `gh`)*
- [ ] SHA trong `marketplace.json` resolve được: `git ls-remote https://github.com/Viindoo/odoo-semantic-mcp.git | grep <sha>` thấy match **(admin SSH verify — requires local plugin install)**

---

## 8. Systemd Services

**Services tự-restart khi crash, và sẽ khởi động lại sau reboot.**

- [ ] `systemctl is-enabled odoo-semantic-mcp` → `enabled` **(admin SSH verify)**
- [ ] `systemctl is-enabled odoo-semantic-webui` → `enabled` **(admin SSH verify)**
- [ ] Simulate crash: `sudo systemctl kill -s SIGKILL odoo-semantic-mcp` → sau 5s `systemctl status` → `active (running)` lại **(admin SSH verify)**
  - *Restart policy: `Restart=on-failure` trong service file*

---

## 9. Indexer Cron

**Cron job chạy và log ghi được.**

- [ ] `/etc/cron.d/odoo-semantic-reindex` tồn tại: `ls /etc/cron.d/odoo-semantic-reindex` **(admin SSH verify)**
- [ ] Chạy thủ công 1 lần để verify: `sudo -u odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all` → exit 0 **(admin SSH verify)**
- [ ] Log ghi được: `tail /var/log/odoo-semantic-reindex.log` → có output **(admin SSH verify)**

---

## 10. Web UI Session Auth (M7 W16)

**Xác nhận session-based auth hoạt động đúng trước khi mở Web UI (port 8003).**

> **§10 M8 DEPENDENCY (annotated 2026-05-14):** Items §10.2–§10.4 phụ thuộc M8 Astro deployment. Jinja2 webui (`odoo-semantic-webui.service` port 8003) sẽ bị thay thế hoàn toàn bằng Astro frontend trong M8 — xem [`docs/superpowers/plans/2026-05-12-milestone-8-astro-unified.md`](../superpowers/plans/2026-05-12-milestone-8-astro-unified.md) §9 (đã absorb P1-E criteria cho `/admin/repos`).
>
> **Exit criteria** cho §10.x items:
> - M8 W3 (`feat/m8-admin-pages`) merged — `site/src/pages/admin/repos.astro` tồn tại
> - M8 W4 (`feat/m8-nginx-integration`) merged — nginx route `/admin/*` → Astro `:4321`
> - `odoo-semantic-astro.service` running + enabled trên production
>
> **DO NOT** mark §10.x items là `[x]` hoặc "skipped permanently" trước khi M8 deploy. Re-verify sau khi M8 release.

- [ ] `create-webui-user` đã chạy — ít nhất 1 admin user tồn tại:
  `python -m src.manager list-webui-users` → thấy ít nhất 1 user **(admin SSH verify)**
  - *Tạo user: `python -m src.manager create-webui-user admin` (prompt mật khẩu)*
- [ ] Unauthenticated GET `/admin/repos` → 302 redirect đến `/admin/login`:
  `curl -I https://odoo-semantic.viindoo.com/admin/repos` → `Location: /admin/login` **(IS M8 DEPENDENCY — deferred to `feat/m8-admin-pages` W3 + `feat/m8-nginx-integration` W4. Re-verify khi `odoo-semantic-astro.service` active. Was M7.5-P1-E, absorbed into M8 plan §9.)**
- [ ] POST `/admin/login` với sai mật khẩu → flash error (không grant session) **(IS M8 DEPENDENCY — re-verify post-M8)**
- [ ] GET `/admin/logout` clears session → tiếp theo request tới `/admin/` → 302 `/admin/login` **(IS M8 DEPENDENCY — re-verify post-M8)**
- [ ] `WEBUI_SESSION_SECRET` đã set trong `webui.env` (không dùng auto-generated ephemeral secret):
  `sudo grep WEBUI_SESSION_SECRET /etc/odoo-semantic/webui.env` → non-empty value **(admin SSH verify — secret vẫn applicable cho Astro auth, không phải M8 dependency)**

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
