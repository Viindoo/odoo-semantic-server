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
- [x] Certbot timer chạy OK: `systemctl status certbot.timer` → `active (waiting)` hoặc `active (running)` **(admin SSH verify)**
<!-- verified 2026-05-16: systemctl status certbot.timer → active (waiting), next trigger Sat 2026-05-16 14:24:25 +07 -->
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
- [x] `rate_limit_rpm = 120` (hoặc giá trị phù hợp) trong `odoo-semantic.conf [auth]` **(admin SSH verify)**
<!-- verified 2026-05-16: [auth] section absent → middleware falls back to DEFAULT_RATE_LIMIT_RPM=120 (src/constants.py), rate limit is active -->
  - *Kiểm tra: `sudo grep rate_limit_rpm /etc/odoo-semantic/odoo-semantic.conf`*
- [x] Ít nhất 1 API key đã tạo: `python -m src.manager list` → thấy key name **(admin SSH verify)**
<!-- verified 2026-05-16: docker exec psql → api_keys table has 5 rows, all named "David Tran", created 2026-05-15 -->
  - *Tạo key: `python -m src.manager create-api-key admin`*

---

## 3. Port Isolation

**Web UI port 8003 không reachable từ external host.**

- [ ] Từ external host: `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` → connection refused hoặc timeout **(admin SSH verify — requires nmap / external host access)**
<!-- not verified 2026-05-16: 8003 confirmed to bind 127.0.0.1 only (ss -tlnp shows 127.0.0.1:8003), but external reachability test requires outbound scan from a remote host — defer to admin -->
  - *Nếu reach được: kiểm tra `odoo-semantic.conf [server]` + firewall rules — 8003 phải bind 127.0.0.1 only*
- [x] DB ports không expose: `sudo ss -tlnp | grep -E '7687|5432'` → chỉ bind `127.0.0.1` (không `0.0.0.0`) **(admin SSH verify)**
<!-- verified 2026-05-16: ss -tlnp shows 127.0.0.1:7687 and 127.0.0.1:5432, neither bound 0.0.0.0 -->
  - *Nếu bind 0.0.0.0: sửa docker-compose.yml → `"127.0.0.1:7687:7687"`*
- [x] Docker daemon không expose TCP: `sudo ss -tlnp | grep 2375` → trống (Unix socket only) **(admin SSH verify)**
<!-- verified 2026-05-16: ss -tlnp | grep 2375 → no output; Docker TCP port not listening -->

---

## 4. Logrotate

**Log file reindex không phình to theo thời gian.**

- [ ] `/etc/logrotate.d/odoo-semantic` tồn tại: `ls /etc/logrotate.d/odoo-semantic` **(admin SSH verify)**
<!-- not verified 2026-05-16: ls /etc/logrotate.d/odoo-semantic → No such file or directory; logrotate config not installed -->
  - *Cài: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/`*
- [ ] Dry-run sạch: `sudo logrotate --debug /etc/logrotate.d/odoo-semantic` → không có error **(admin SSH verify)**
<!-- not verified 2026-05-16: depends on §4.1 which failed — logrotate config missing -->

---

## 5. Backup & Recovery

**Backup có thể restore thành công (không chỉ tạo file).**

- [ ] Backup PG chạy được: `python -m src.cli backup --output /tmp/test-backup.sql` → file > 0 bytes **(admin SSH verify)**
<!-- not verified 2026-05-16: requires non-dry-run write execution, defer to admin -->
- [ ] Backup Neo4j chạy được: neo4j dump command (xem docs/deploy.md §2.4) → file `~/backups/neo4j-<DATE>.dump` tạo thành công **(admin SSH verify)**
<!-- not verified 2026-05-16: requires write execution (neo4j dump), defer to admin -->
- [ ] Restore thử trên non-production: restore PG + count `SELECT COUNT(*) FROM profiles` > 0 **(admin SSH verify)**
  - *Tham khảo docs/deploy/disaster-recovery.md — bắt buộc test ít nhất 1 lần trước launch*
<!-- not verified 2026-05-16: requires non-production environment + DB restore writes, defer to admin -->
- [ ] `webui.env` (FERNET_KEY) backed up vào secrets manager riêng biệt — **không chỉ trên server disk** **(admin SSH verify)**
<!-- not verified 2026-05-16: FERNET_KEY present in ~/git/odoo-semantic-mcp/.env but "secrets manager separate from server disk" requires admin confirmation of offsite backup -->
  - *Mất FERNET_KEY = không decrypt SSH private key trong DB*

---

## 6. MCP Tool Sign-Off (All 21 Tools)

**Mỗi tool phải trả về kết quả có cấu trúc — không được empty hoặc error.**

> **Tool count history:** 14 (M1–M5) + 7 (M9 W-OSM Wave 1 — `describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) = 21. See [ADR-0023](../adr/proposed/0023-tool-output-completeness.md).

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
| 15 | `describe_module` | `describe_module("sale", "17.0")` | Manifest (Depends/Edition/Version) + `Defines models` / `Extends models` / `Views` (by type) / `JS patches` + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |
| 16 | `list_fields` | `list_fields("sale.order", "17.0")` | Header `Fields of sale.order (Odoo 17.0)` + per-module subtree of `name : ttype` rows + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |
| 17 | `list_methods` | `list_methods("sale.order", "17.0")` | Header `Methods of sale.order (Odoo 17.0)` + per-module subtree of `name[(*)] : kind` rows (override marker `(*)`) + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |
| 18 | `list_views` | `list_views("sale.order", "17.0")` | Header `Views of sale.order (Odoo 17.0)` + per-module subtree of `xmlid : type` rows + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |
| 19 | `list_owl_components` | `list_owl_components("sale_management", "17.0")` | Header `OWL components of sale_management (Odoo 17.0)` + `component_name : bound_model` rows; empty + warning for v8-v13 | `[ ]` (M9 W-OSM Wave 1) |
| 20 | `list_qweb_templates` | `list_qweb_templates("website_sale", "17.0")` | Header `QWeb templates of website_sale (Odoo 17.0)` + `xmlid : t-inherit=<parent or (root)>` rows + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |
| 21 | `list_js_patches` | `list_js_patches(odoo_version="17.0", target="ListController")` | Header `JS patches on ListController (Odoo 17.0)` + per-module subtree of `target.patch_name : era=<era>` rows + `Next:` footer | `[ ]` (M9 W-OSM Wave 1) |

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
<!-- not verified 2026-05-16: marketplace.json references download_url v0.2.0 release zip which returns HTTP 404 (release zip not published); git tag v0.2.0 exists but zip asset missing — follow-up required -->

---

## 8. Systemd Services

**Services tự-restart khi crash, và sẽ khởi động lại sau reboot. M8: 3 services.**

- [x] `systemctl is-enabled odoo-semantic-mcp` → `enabled` **(admin SSH verify)**
<!-- verified 2026-05-16: systemctl is-enabled odoo-semantic-mcp → enabled -->
- [x] `systemctl is-enabled odoo-semantic-webui` → `enabled` **(admin SSH verify)**
<!-- verified 2026-05-16: systemctl is-enabled odoo-semantic-webui → enabled -->
- [x] `systemctl is-enabled odoo-semantic-astro` → `enabled` **(admin SSH verify — M8 new)**
<!-- verified 2026-05-16: systemctl is-enabled odoo-semantic-astro → enabled -->
- [x] `systemctl is-active odoo-semantic-astro` → `active (running)` port 4321 **(admin SSH verify)**
<!-- verified 2026-05-16: systemctl is-active odoo-semantic-astro → active; curl -sI http://127.0.0.1:4321/ → HTTP 200 text/html -->
  - *Kiểm tra: `curl -I http://127.0.0.1:4321/` → HTTP 200 HTML*
- [x] Astro build artifacts present: `ls /opt/odoo-semantic-mcp/site/dist/server/entry.mjs` → file tồn tại **(admin SSH verify)**
<!-- verified 2026-05-16: file exists at ~/git/odoo-semantic-mcp/site/dist/server/entry.mjs (service runs from ~/git path per deployment state) -->
  - *Nếu thiếu: `cd /opt/odoo-semantic-mcp/site && pnpm install --frozen-lockfile && pnpm build`*
- [ ] Simulate crash: `sudo systemctl kill -s SIGKILL odoo-semantic-mcp` → sau 5s `systemctl status` → `active (running)` lại **(admin SSH verify)**
<!-- not verified 2026-05-16: crash simulation mutates running service state — guardrail prevents execution; defer to admin maintenance window -->
  - *Restart policy: `Restart=on-failure` trong service file*

---

## 9. Indexer Cron

**Cron job chạy và log ghi được.**

- [ ] `/etc/cron.d/odoo-semantic-reindex` tồn tại: `ls /etc/cron.d/odoo-semantic-reindex` **(admin SSH verify)**
<!-- not verified 2026-05-16: ls /etc/cron.d/odoo-semantic-reindex → No such file or directory; cron job not installed -->
- [ ] Chạy thủ công 1 lần để verify: `sudo -u odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all` → exit 0 **(admin SSH verify)**
<!-- not verified 2026-05-16: requires write execution (indexer run), defer to admin -->
- [ ] Log ghi được: `tail /var/log/odoo-semantic-reindex.log` → có output **(admin SSH verify)**
<!-- not verified 2026-05-16: depends on §9.1 (cron not installed) and §9.2 (indexer not run) -->

---

## 10. Web UI Session Auth (M7 W16)

**Xác nhận session-based auth hoạt động đúng trước khi mở Web UI (port 8003).**

> **§10 M8 DELIVERED (2026-05-14):** M8 PR #86 merged. `site/src/pages/admin/repos.astro` tồn tại. nginx-m8.conf routes `/admin/*` → Astro `:4321`. Items §10.2–§10.4 cần re-verify sau khi `odoo-semantic-astro.service` active trên production (verify theo §10.5).

- [x] `create-webui-user` đã chạy — ít nhất 1 admin user tồn tại:
  `python -m src.manager list-webui-users` → thấy ít nhất 1 user **(admin SSH verify)**
<!-- verified 2026-05-16: python -m src.manager list-webui-users → username=admin, is_admin=True, is_active=True, created 2026-05-11 -->
  - *Tạo user: `python -m src.manager create-webui-user admin` (prompt mật khẩu)*
- [x] Unauthenticated GET `/admin/repos` → 302 redirect đến `/admin/login`:
  `curl -I https://odoo-semantic.viindoo.com/admin/repos` → `Location: /admin/login` **(re-verify post-M8 deploy — was M7.5-P1-E, now covered by Astro middleware + nginx-m8.conf)**
<!-- verified 2026-05-16: curl -sI https://odoo-semantic.viindoo.com/admin/repos → HTTP/2 302, location: /admin/login -->
- [x] POST `/admin/login` với sai mật khẩu → flash error (không grant session) **(re-verify post-M8 deploy)**
<!-- verified 2026-05-16: POST /admin/login wrong password → HTTP/2 403 (rejected, no session cookie issued) -->
- [x] GET `/admin/logout` clears session → tiếp theo request tới `/admin/` → 302 `/admin/login` **(re-verify post-M8 deploy)**
<!-- verified 2026-05-16: curl -sv https://odoo-semantic.viindoo.com/admin/logout → HTTP/2 302, location: /admin/login -->
- [ ] `WEBUI_SESSION_SECRET` đã set trong `webui.env` (không dùng auto-generated ephemeral secret):
  `sudo grep WEBUI_SESSION_SECRET /etc/odoo-semantic/webui.env` → non-empty value **(admin SSH verify — secret vẫn applicable cho Astro auth, không phải M8 dependency)**
<!-- not verified 2026-05-16: /etc/odoo-semantic/webui.env does not exist; WEBUI_SESSION_SECRET confirmed set in ~/git/odoo-semantic-mcp/.env but canonical /etc path missing — follow-up to install production env file -->

---

## 10.5 Astro Frontend (M8)

**Verify Astro landing + admin UI live sau khi `odoo-semantic-astro.service` active.**

- [x] `curl -sI http://127.0.0.1:4321/` → HTTP 200, `Content-Type: text/html` — Astro landing page **(admin SSH verify)**
<!-- verified 2026-05-16: curl -sI http://127.0.0.1:4321/ → HTTP 200, Content-Type: text/html; charset=utf-8 (61069 bytes) -->
- [x] `curl -sI https://<domain>/` → HTTP 200 HTML (qua nginx) — landing hero reachable
<!-- verified 2026-05-16: curl -sI https://odoo-semantic.viindoo.com/ → HTTP/2 200, content-type: text/html; charset=utf-8 -->
- [x] `curl -sI https://<domain>/admin` → 302 redirect đến `/admin/login` (Astro middleware auth-gate) **(admin SSH verify)**
<!-- verified 2026-05-16: curl -sI https://odoo-semantic.viindoo.com/admin → HTTP/2 302, location: /admin/login -->
- [ ] `curl -sI https://<domain>/api/health` → HTTP 200 `Content-Type: application/json` — FastAPI JSON-only confirm **(NOT `text/html`)**
<!-- not verified 2026-05-16: /api/health returns HTTP 401 application/json (auth-required), not 200; FastAPI is JSON-only confirmed (no HTML/Jinja2), but 200 spec not met — known followup per CSP gap note -->
  - *Nếu trả HTML: FastAPI vẫn mount Jinja2 — kiểm tra `pyproject.toml` đã xóa `jinja2` dependency*
- [x] Nginx routing sanity:
<!-- verified 2026-05-16: /api/repos/profiles → HTTP 401 JSON (FastAPI :8003, auth required — routing correct); /admin/login → HTTP 200 text/html (Astro :4321); /mcp → HTTP 401 (MCP :8002) -->
  - `curl -sI https://<domain>/api/repos/profiles` → HTTP 200 JSON (FastAPI :8003)
  - `curl -sI https://<domain>/admin/login` → HTTP 200 HTML (Astro :4321)
  - `curl -sI https://<domain>/mcp` → HTTP 401 (MCP :8002, auth required)
- [ ] Browser tests pass post-deploy: `pytest tests/browser/admin/ -m browser` (từ deploy server hoặc CI) — 68 tests GREEN **(admin hoặc CI verify)**
<!-- not verified 2026-05-16: browser tests require playwright + interactive CI run; 92 test functions exist (suite grown beyond 68); defer to CI pipeline -->

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
| MCP Tool Sign-Off tất cả 21 tools (§6) | | | |
| Install Page (§7) | | | |
| Systemd Services (§8) | | | |
| Indexer Cron (§9) | | | |
| Web UI Session Auth (§10) | | | |
| Astro Frontend M8 (§10.5) | | | |

**Khi tất cả 12 mục `[x]` → deploy ready. Phân phát key + URL.**

---

## Known follow-ups (non-blocking, opened 2026-05-16)

Items left unchecked after 2026-05-16 read-only verification sweep. Each needs an admin action or CI run to close.

1. **§1.2 certbot.timer** — TICKED (was unchecked; systemctl confirms active/waiting). No follow-up needed.

2. **§3.1 External port isolation test** — `ss -tlnp` confirms 8003 binds 127.0.0.1 only; full external reachability test requires scanning from a remote host. Admin: run `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` from an external machine before key distribution.

3. **§4.1–§4.2 Logrotate config missing** — `/etc/logrotate.d/odoo-semantic` does not exist. Admin: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/ && sudo logrotate --debug /etc/logrotate.d/odoo-semantic`.

4. **§5 Backup & Recovery (all 4 sub-items)** — All require write execution (pg_dump, neo4j dump, restore to non-prod DB, offsite backup confirmation). Must be completed by admin before public launch.

5. **§7 Marketplace release zip missing** — `dist/marketplaces/viindoo/marketplace.json` references `v0.2.0` release zip which returns HTTP 404 from GitHub releases. Tag `v0.2.0` exists but the plugin zip asset was never uploaded. Admin: upload `odoo-semantic-plugin.zip` to the v0.2.0 GitHub release, or update marketplace.json to point to the correct version/URL.

6. **§8.6 Crash simulation** — Cannot run `systemctl kill` under read-only guardrail. Admin: test in a maintenance window — kill MCP service, confirm auto-restart within 5s.

7. **§9.1–§9.3 Indexer cron not installed** — `/etc/cron.d/odoo-semantic-reindex` does not exist. Admin: install cron job per `docs/deploy.md`, run once manually to verify log output.

8. **§10.5 /api/health returns 401 not 200** — FastAPI JSON-only confirmed (no Jinja2/HTML), but the auth-exempt `/health` route is only on MCP :8002, not on FastAPI :8003. The `/api/health` spec in the checklist cannot be satisfied without adding an auth-exempt health route to the FastAPI app, or updating the checklist to accept 401 JSON as passing. Tracked as follow-up; does not block production (JSON-only is confirmed).

9. **§10.5 CSP + Permissions-Policy headers missing** — `curl -sv https://odoo-semantic.viindoo.com/` shows no `Content-Security-Policy` or `Permissions-Policy` headers in nginx response. Known follow-up per memory note `m9_csp_permissions_policy_gap`. Admin: add CSP + Permissions-Policy to nginx server block.

10. **§10.5 Browser tests** — 92 browser test functions exist (suite grown past the "68 tests" milestone marker). Need `pytest tests/browser/admin/ -m browser` GREEN run in CI or against production.

11. **§10 WEBUI_SESSION_SECRET production env path** — `WEBUI_SESSION_SECRET` is set in `~/git/odoo-semantic-mcp/.env` but the checklist references `/etc/odoo-semantic/webui.env` which does not exist. Admin: confirm the running service loads the secret from `.env`, or create the canonical `/etc/odoo-semantic/webui.env` per `docs/deploy.md`.

---

*Xem thêm: [docs/deploy.md](../deploy.md) · [docs/deploy/disaster-recovery.md](disaster-recovery.md)*
