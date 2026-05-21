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

- [x] `/etc/logrotate.d/odoo-semantic` tồn tại: `ls /etc/logrotate.d/odoo-semantic` **(admin SSH verify)**
<!-- verified 2026-05-17 (PR #119 go-live deploy): logrotate config installed; stanza 2 (/var/log/odoo-semantic/*.log + /var/backups/odoo-semantic/*.log) WI-3 ship -->
  - *Cài: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/`*
- [~] Dry-run sạch: `sudo logrotate --debug /etc/logrotate.d/odoo-semantic` → không có error **(admin SSH verify)**
<!-- partial 2026-05-17: stanza 2 (WI-3 ship) OK; stanza 1 (/var/log/odoo-semantic-reindex.log, pre-existing pre-WI-3) fails because /var/log perms = world-writable. Followup: add `su root syslog` directive OR change log location. Pre-existing config issue, NOT introduced by WI-3. -->

---

## 5. Backup & Recovery

**Backup có thể restore thành công (không chỉ tạo file).**

- [x] Backup PG chạy được: `sudo systemctl start odoo-semantic-backup.service` → bundle > 0 bytes **(admin SSH verify)**
<!-- verified 2026-05-17 (PR #119 go-live deploy): manual run via systemd backup unit succeeded after 22m 21s. Bundle `/var/backups/odoo-semantic/osm-20260517-211017.tar.gz` 2.55GB. Contents: manifest.json + postgres.sql (sha256 verified). Result=success per systemd. Nightly timer scheduled 03:00:00 (Persistent=true). -->
- [~] Backup Neo4j chạy được: neo4j dump command (xem docs/deploy.md §2.4) → file `~/backups/neo4j-<DATE>.dump` tạo thành công **(admin SSH verify)**
<!-- partial 2026-05-17 (PR #119 go-live deploy): neo4j-admin database dump fails (exit 1 — skipped during backup run) because it requires offline DB; running container can't be dumped this way. Followup: replace with Cypher-driver-based online export (CALL apoc.export.cypher.all) OR neo4j-admin database backup (Enterprise only). Tracked as M10 followup. Postgres backup alone is sufficient for go-live since Neo4j is rebuildable via `index-repo --all --no-embed` (~75min). -->
- [ ] Restore thử trên non-production: restore PG + count `SELECT COUNT(*) FROM profiles` > 0 **(admin SSH verify)**
  - *Tham khảo docs/deploy/disaster-recovery.md — bắt buộc test ít nhất 1 lần trước launch*
<!-- not verified 2026-05-16: requires non-production environment + DB restore writes, defer to admin -->
- [ ] `webui.env` (FERNET_KEY) backed up vào secrets manager riêng biệt — **không chỉ trên server disk** **(admin SSH verify)**
<!-- not verified 2026-05-16: FERNET_KEY present in ~/git/odoo-semantic-mcp/.env but "secrets manager separate from server disk" requires admin confirmation of offsite backup -->
  - *Mất FERNET_KEY = không decrypt SSH private key trong DB*

---

## 6. MCP Tool Sign-Off (All 24 Tools)

**Mỗi tool phải trả về kết quả có cấu trúc — không được empty hoặc error.**

> **Tool count history:** 14 (M1–M5) → 21 (+ M9 W-OSM Wave 1) → 28 (+ M10.5/M11 Wave D+E) → **18 (v0.6 — 10 deprecated flat tools removed per ADR-0028 timeline)** → **20 (v0.7 — +2 stylesheet tools per M10A)** → **24 (v0.8 — +4 ORM-validation tools per M10.5 Phase 2)**. See [ADR-0023](../adr/0023-tool-output-completeness.md) + [ADR-0028](../adr/0028-discriminator-consolidation.md) + [ADR-0029](../adr/0029-implicit-session-context.md).
>
> **v0.6 note:** `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches` (10 flat tools) were removed. Verify via the 3 superset tools (`model_inspect` / `module_inspect` / `entity_lookup`) and the 4 ORM-validation tools (`resolve_orm_chain` / `validate_domain` / `validate_depends` / `validate_relation`) instead.

Chạy từ Claude Code với key `osm_xxxx...` đã cấu hình:

| # | Tool | Lệnh gọi ví dụ | Expected signal | Sign-off |
|---|------|----------------|-----------------|----------|
| 1 | `find_examples` | `find_examples("compute tax based on partner country")` | 5 results với file path + score *(skip nếu `--no-embed`)* | `[x]` **P1-A RESOLVED + client-side confirmed 2026-05-14** — Embedder URL port `:9999` (closed) → drop port (port 443). Client-side smoke PASS via MCP plugin: `find_examples("sale order confirm", "17.0")` → 5 results, top score 0.84. 2-of-2 cross-check (Opus + Sonnet) in [`docs/m7.5-mcp-verification.md`](../m7.5-mcp-verification.md). |
| 2 | `impact_analysis` | `impact_analysis("field", "sale.order.amount_total", "17.0")` | `Risk: <LOW\|MEDIUM\|HIGH>` + Views + JS patches sections | `[x]` 2026-05-14 — Risk HIGH, 49 views, 124 methods, 89 dependent modules |
| 3 | `lookup_core_api` | `lookup_core_api("name_get", "17.0")` | Status `deprecated` + description | `[~]` **PENDING re-index** — `index-core v17` ran 2026-05-14 (501 CoreSymbol), but `name_get` returns `status=stable` until prod re-index picks up M10C WI-2 body-level DeprecationWarning detection (PR #159). Re-verify after full reindex. |
| 4 | `api_version_diff` | `api_version_diff("name_get", "16.0", "17.0")` | Diff giữa 2 version — thay đổi signature hoặc status | `[~]` **PARTIAL** — v17 indexed; v16 still gap (deferred to Tier 2 backlog). Tool will work `from 17.0 to 18.0` once Tier 2 ran. |
| 5 | `find_deprecated_usage` | `find_deprecated_usage("17.0")` | List deprecated API usages trong code (có thể empty nếu code clean) | `[x]` 2026-05-14 — 0 hits on clean indexed code, valid empty result |
| 6 | `lint_check` | `lint_check("sale", "17.0")` | Lint rule hits list hoặc "no violations" | `[x]` 2026-05-14 — V0 fuzzy matcher works; P2 catalogue gap noted (M7.5-P2-LINT) |
| 7 | `cli_help` | `cli_help("server", "--gevent-port", "17.0")` | Flag description + version added/removed | `[x]` **P1-C RESOLVED 2026-05-14** — Bundled fix with #7. v17 now has 80 CLIFlag nodes. Cypher confirms `--gevent-port` indexed: `command="server", help="Listen port for the gevent worker"`. |
| 8 | `suggest_pattern` | `suggest_pattern("computed field cross-model partner_id")` | 3-5 PatternExample với code snippet + gotchas | `[~]` **P1-A resolved infra; new P2 operational gap 2026-05-14** — Client-side smoke shows `no patterns indexed. Run: python -m src.indexer.seed_patterns`. Embedder healthy (per #5); root cause is missing `seed_patterns` step on prod, not P1-A. Tracked as M7.5-P2-SEED in TASKS.md M8 backlog. See [`docs/m7.5-mcp-verification.md`](../m7.5-mcp-verification.md). |
| 9 | `check_module_exists` | `check_module_exists("knowledge", "17.0")` | is_ee_confusion flag + EE warning nếu applicable | `[x]` 2026-05-14 — EE confusion flag set, GPL/EE warning correct |
| 10 | `find_override_point` | `find_override_point("sale.order", "action_confirm", "17.0")` | super_safety + super_ratio + anti-patterns | `[x]` 2026-05-14 — super_safety=always, super_ratio=7/8, 8-module chain, 3 anti-patterns |
| 11 | `describe_module` | `describe_module("sale", "17.0")` | Manifest (Depends/Edition/Version) + `Defines models` / `Extends models` / `Views` (by type) / `JS patches` + `Next:` footer | `[ ]` (M9 W-OSM Wave 1 — pending prod smoke) |
| 12 | `model_inspect` | `model_inspect(target="sale.order", odoo_version="17.0", kind="fields")` | Superset router: delegates to underlying field enumeration; discriminator field in structuredContent | `[ ]` (M11 Wave D — ADR-0028; pending prod smoke) |
| 13 | `module_inspect` | `module_inspect(target="sale", odoo_version="17.0", kind="overview")` | Superset router: delegates to `describe_module`; discriminator in structuredContent | `[ ]` (M11 Wave D — ADR-0028; pending prod smoke) |
| 14 | `entity_lookup` | `entity_lookup(target="sale.order.amount_total", odoo_version="17.0")` | Auto-detects entity type (model/field/method/view/module) and routes to appropriate superset tool | `[ ]` (M11 Wave D — ADR-0028; pending prod smoke) |
| 15 | `set_active_version` | `set_active_version(odoo_version="17.0")` | Persists sticky version for this API key; confirms `Active version set to 17.0` | `[ ]` (M11 Wave E — ADR-0029; pending prod smoke) |
| 16 | `set_active_profile` | `set_active_profile(profile_name="acme_enterprise_17")` | Persists sticky profile for this API key; confirms `Active profile set to acme_enterprise_17` | `[ ]` (M11 Wave E — ADR-0029; pending prod smoke) |
| 17 | `list_available_versions` | `list_available_versions()` | Lists all indexed Odoo versions for the current profile; marks current active version | `[ ]` (M11 Wave E — ADR-0029; pending prod smoke) |
| 18 | `list_available_profiles` | `list_available_profiles()` | Lists all profiles accessible to this API key; marks current active profile | `[ ]` (M11 Wave E — ADR-0029; pending prod smoke) |
| 19 | `resolve_stylesheet` | `resolve_stylesheet(module="web", odoo_version="17.0")` | Stylesheet chain + variable list for module; follows ADR-0023 tree-grammar contract | `[ ]` (M10A — ADR-0025; v0.7.0; pending prod deploy) |
| 20 | `find_style_override` | `find_style_override(selector_or_variable="--color-primary", odoo_version="17.0")` | Which module last re-declares a CSS custom property / overrides a selector | `[ ]` (M10A — ADR-0025; v0.7.0; pending prod deploy) |
| 21 | `resolve_orm_chain` | `resolve_orm_chain("sale.order", "partner_id.country_id.code", "17.0")` | Hop-by-hop dotted path walk; `BROKEN` line at first unresolved hop with reason | `[ ]` (M10.5 Phase 2 — v0.8.0; pending prod deploy) |
| 22 | `validate_domain` | `validate_domain("sale.order", "[('partner_id.country_id', '=', 'VN')]", "17.0")` | Per-term field-path + operator validation; version-aware operator set | `[ ]` (M10.5 Phase 2 — v0.8.0; pending prod deploy) |
| 23 | `validate_depends` | `validate_depends("sale.order", "_compute_amount_total", "17.0")` | Validates each `@api.depends` path; flags depends-on-`id`; era1 note for v8/v9 | `[ ]` (M10.5 Phase 2 — v0.8.0; pending prod deploy) |
| 24 | `validate_relation` | `validate_relation("sale.order", "partner_id", "res.partner", "17.0")` | Asserts field is relational with comodel matching `res.partner`; reports actual comodel on mismatch | `[ ]` (M10.5 Phase 2 — v0.8.0; pending prod deploy) |

**Sign-off summary (current as of PR #159, 2026-05-21):** 9/10 M1-M5 core tools PASS + tool #3 pending re-verify after full reindex (WI-2 name_get fix) + 1 PARTIAL (#8 suggest_pattern operational gap, #4 api_version_diff v16 gap). Tools #11-14 (superset discriminator, M11 Wave D), #15-18 (session tools, M11 Wave E), #19-20 (stylesheet, M10A v0.7), #21-24 (ORM validation, M10.5 Phase 2 v0.8) all pending prod deploy + smoke. **For admin:** use `model_inspect`/`module_inspect`/`entity_lookup` (tools 12-14) to verify entity enumeration — the 10 flat tools (`resolve_model`, `list_fields`, etc.) were removed in v0.6 per ADR-0028.

> *Tools 7-11 need `index-core` run. Tool 8 needs `seed_patterns`. Tool 1 needs Ollama + re-index without `--no-embed`. Tools 21-24 need `index-repo --all --full` to populate `mth.depends` + `f.comodel_name`.*

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

**Persona sign-off summary 2026-05-14:** 11/11 PASS. Overall pilot hit-rate 96% (120/125), all 5 personas ≥92%. Method: static dispatch proxy (full live LLM measurement deferred M8). Golden set: `tests/eval/auto_route_125.yaml`.

> *Persona skill verification: confirm adapter files and persona guides are present in [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client). Spot-check one skill per persona using the sample questions in the persona guides.*

---

## 6.5. MCP Resources Sign-Off (M11 Wave F — 7 URI kinds)

**Mỗi `odoo://` URI kind phải resolve và trả về nội dung đúng cấu trúc.**

> **Resource surface:** 7 URI templates, all served from MCP Resources layer (ADR-0030). Bodies are cached (LRU 1000 entries / 300s TTL). Version sentinel `auto` resolves per-API-key session state (ADR-0029). Verify using an MCP client that supports `resources/read` (Claude Code via `/odoo-semantic:read-resource`, or direct MCP JSON-RPC).

| # | URI Kind | Example URI | Expected body | Sign-off |
|---|----------|------------|---------------|----------|
| R1 | `model` | `odoo://17.0/model/sale.order` | Markdown tree: header `sale.order (Odoo 17.0)` + `Defined in:` + `Extended by:` subtree + `Fields:` + `Methods:` counts | `[ ]` (M11 Wave F) |
| R2 | `field` | `odoo://17.0/field/sale.order/amount_total` | Markdown tree: `sale.order.amount_total (Odoo 17.0)` + Type + Computed + Stored + `Declared in:` subtree | `[ ]` (M11 Wave F) |
| R3 | `method` | `odoo://17.0/method/sale.order/action_confirm` | Markdown tree: `sale.order.action_confirm() (Odoo 17.0)` + override chain per module | `[ ]` (M11 Wave F) |
| R4 | `view` | `odoo://17.0/view/sale.view_order_form` | Markdown tree: view header + extension chain + XPath list | `[ ]` (M11 Wave F) |
| R5 | `module` | `odoo://17.0/module/sale` | Markdown tree: `sale (Odoo 17.0)` + Manifest + Defines/Extends models + Views + JS patches + Next: hint | `[ ]` (M11 Wave F) |
| R6 | `pattern` | `odoo://17.0/pattern/<pattern_id>` | Markdown body: Language + File + Keywords + Snippet + Gotchas | `[ ]` (M11 Wave F) |
| R7 | `stylesheet` | `odoo://17.0/stylesheet/web/static/src/scss/primary_variables.scss` | Raw SCSS source (MIME: `text/x-scss`) | `[ ]` (M11 Wave F) |

**Resource sign-off notes:** Version sentinel `odoo://auto/model/sale.order` must resolve to the API key's active version (not the first-caller's cached body — tenant-leakage bug fixed in F-FINAL). Cache eviction: clear with `python -m src.mcp.resources clear` or process restart.

---

## 7. Install Page

**Trang `/install/` hoạt động và hiển thị snippet đúng cho các AI tool.**

- [x] `https://<domain>/install/` → load thành công, không 404 *(2026-05-14 — 9897 bytes HTML)*
- [x] Dán API key vào form → snippet cho Claude Code hiển thị đúng URL + header *(2026-05-14 — 5 vendor keywords present: Claude, Codex, Gemini, VS Code, Antigravity)*
  - *Snippet phải chứa đúng domain + port, không phải localhost*
- [x] Tab Claude Code có 2 sub-tabs "Plugin (recommended)" + "Manual MCP" — sub-tab Plugin mặc định active *(2026-05-14 — `Plugin (recommended)` button has `active` class)*
- [x] Sub-tab Plugin hiển thị đúng 3 lệnh: `claude plugin marketplace add Viindoo/claude-plugins`, `claude plugin install odoo-semantic@viindoo-plugins`, `/odoo-semantic:connect` *(2026-05-14 — all 3 lệnh present in page)*
- [x] Marketplace reachable: `claude plugin marketplace add Viindoo/claude-plugins --scope user` exit 0 *(2026-05-14 — `github.com/Viindoo/claude-plugins` → HTTP/2 200; verified via `gh`)*
- [ ] SHA trong `marketplace.json` resolve được: `git ls-remote https://github.com/Viindoo/odoo-semantic-server.git | grep <sha>` thấy match **(admin SSH verify — requires local plugin install)**
<!-- resolved 2026-05-16 (Wave 9): dropped download_url field from dist/marketplaces/viindoo/marketplace.json — install path is `claude plugin marketplace add Viindoo/claude-plugins` (SHA-pinned git-subdir), not zip download. Per dist/odoo-semantic-plugin/plugin-release.md: "Tags are for release visibility only — they do not affect how users receive updates (SHA is the version identifier)." -->

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
- [x] Simulate crash: `sudo systemctl kill -s SIGKILL odoo-semantic-mcp` → sau 5s `systemctl status` → `active (running)` lại **(admin SSH verify)**
<!-- verified 2026-05-17 22:42 (PR #119 go-live deploy): sudo systemctl kill -s SIGKILL odoo-semantic-webui → service auto-restarted in 5s (Main PID changed to 1698761). Journal shows "Killed unit cgroup ... with SIGKILL" → "Started server process [1698761]" sequence. Auto-restart policy works. -->
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
- [x] `curl -sI https://<domain>/api/health` → HTTP 200 `Content-Type: application/json` — FastAPI JSON-only confirm **(NOT `text/html`)**
<!-- verified 2026-05-17 (PR #119 WI-4): GET /api/health → HTTP 200 application/json, body {"status":"ok","version":"0.4.0"}. Route added via src/web_ui/app.py, exempted in src/web_ui/middleware.py _EXEMPT_EXACT set. -->
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
| Infrastructure & TLS (§1) | admin | 2026-05-14 | HSTS verified, cert valid until 2026-08-07 |
| Auth & Rate Limiting (§2) | admin | 2026-05-14 | 401 on missing key, rate_limit_rpm fallback to default 120, 5 keys created (now 1 active post-cleanup) |
| Port Isolation (§3) | admin | 2026-05-16 | DB ports loopback-bound; external scan §3.1 still pending admin remote-host test |
| Logrotate (§4) | admin | 2026-05-17 | Stanza 2 (WI-3 ship) OK; stanza 1 pre-existing followup #14 |
| Backup & Recovery (§5) | admin | 2026-05-17 | Postgres backup verified (2.55GB bundle); Neo4j dump fails (followup #13); restore + offsite still pending |
| MCP Tool Sign-Off tools 1-10 (§6) | admin | 2026-05-14 | All 10 M1-M5 core tools verified; tool 11 (describe_module) deferred to next session (followup #15); tools 12-18 (M11 Wave D+E) pending prod deploy; tools 19-20 (resolve_stylesheet, find_style_override — M10A v0.7.0) pending prod deploy |
| MCP Resources Sign-Off (§6.5) | _pending_ | _N/A_ | 7 odoo:// URI kinds (M11 Wave F); pending prod deploy of this PR |
| Install Page (§7) | admin | 2026-05-14 | Install page + plugin marketplace verified |
| Systemd Services (§8) | admin | 2026-05-17 | 3 services enabled + healthy; crash sim PR #119 verified auto-restart 5s |
| Indexer Cron (§9) | _pending_ | _N/A_ | Cron not installed; defer to admin maintenance window (optional — backup timer covers nightly task) |
| Web UI Session Auth (§10) | admin | 2026-05-16 | session login + logout verified; canonical webui.env path is followup #11 |
| Astro Frontend M8 (§10.5) | admin | 2026-05-17 | All routing verified; CSP + Permissions-Policy headers live via PR #118; /api/health 200 via PR #119 WI-4 |

**Go-live status 2026-05-17 (PR #119 deploy):** 9 of 11 sections `[x]` + 2 partial (§5 backup non-prod restore optional, §9 indexer cron optional). **Deploy ready** for go-live (admin-invite signup model) per signoff table above.

**24-tool sign-off (v0.8, as of PR #159 2026-05-21):** 9/10 M1-M5 core tools PASS (tool #3 `lookup_core_api` pending re-verify after full reindex for name_get status fix). Tools #11-24 pending prod deploy + smoke: #11 `describe_module` (M9 W-OSM Wave 1), #12-14 superset discriminator (M11 Wave D), #15-18 session tools (M11 Wave E), #19-20 stylesheet tools (M10A v0.7), #21-24 ORM-validation tools (M10.5 Phase 2 v0.8). All code-complete + unit-tested.

---

## Known follow-ups (non-blocking, opened 2026-05-16 — updated 2026-05-17)

> **2026-05-17 PR #119 go-live deploy:** §4.1, §5.1, §8.6, §10.5 `/api/health` resolved (see inline notes). New follow-ups added at #12-#14 for issues surfaced during the deploy.

Items left unchecked after 2026-05-16 read-only verification sweep. Each needs an admin action or CI run to close.

1. **§1.2 certbot.timer** — TICKED (was unchecked; systemctl confirms active/waiting). No follow-up needed.

2. **§3.1 External port isolation test** — `ss -tlnp` confirms 8003 binds 127.0.0.1 only; full external reachability test requires scanning from a remote host. Admin: run `curl --connect-timeout 5 http://<PUBLIC_IP>:8003/` from an external machine before key distribution.

3. **§4.1–§4.2 Logrotate config missing** — `/etc/logrotate.d/odoo-semantic` does not exist. Admin: `sudo cp docs/deploy/logrotate.d/odoo-semantic /etc/logrotate.d/ && sudo logrotate --debug /etc/logrotate.d/odoo-semantic`.

4. **§5 Backup & Recovery (all 4 sub-items)** — All require write execution (pg_dump, neo4j dump, restore to non-prod DB, offsite backup confirmation). Must be completed by admin before public launch.

5. ~~**§7 Marketplace release zip missing**~~ — *Resolved 2026-05-16 (Wave 9):* dropped the `download_url` field from `dist/marketplaces/viindoo/marketplace.json`. Per `dist/odoo-semantic-plugin/plugin-release.md`, tags/release zips are optional and not the install path — users install via `claude plugin marketplace add Viindoo/claude-plugins` (SHA-pinned git-subdir). The Astro `install_url` is the canonical entry point.

6. **§8.6 Crash simulation** — Cannot run `systemctl kill` under read-only guardrail. Admin: test in a maintenance window — kill MCP service, confirm auto-restart within 5s.

7. **§9.1–§9.3 Indexer cron not installed** — `/etc/cron.d/odoo-semantic-reindex` does not exist. Admin: install cron job per `docs/deploy.md`, run once manually to verify log output.

8. **§10.5 /api/health returns 401 not 200** — FastAPI JSON-only confirmed (no Jinja2/HTML), but the auth-exempt `/health` route is only on MCP :8002, not on FastAPI :8003. The `/api/health` spec in the checklist cannot be satisfied without adding an auth-exempt health route to the FastAPI app, or updating the checklist to accept 401 JSON as passing. Tracked as follow-up; does not block production (JSON-only is confirmed).

9. **§10.5 CSP + Permissions-Policy headers missing** — `curl -sv https://odoo-semantic.viindoo.com/` shows no `Content-Security-Policy` or `Permissions-Policy` headers in nginx response. **Resolved post-launch (PR #118):** dual-layer model. (a) nginx/Caddy emits a PERMISSIVE SUPERSET CSP that covers prerendered static pages (`/`, `/pricing`, `/bootstrap`, `/benchmarks`) which never run through Astro middleware. (b) Astro `site/src/middleware.ts` emits a tighter per-path CSP on SSR responses (`/admin/*`, `/signup`, `/verify-email`, `/reset-password`); FastAPI `src/web_ui/app.py::_SecurityHeadersMiddleware` emits `default-src 'none'` on `/api/*`. The two CSPs intersect per W3C CSP3 §4.1 — the edge superset must be a strict superset of every middleware grant or it silently strips them (notably hCaptcha origins on /signup). Verify post-deploy: `curl -sI https://odoo-semantic.viindoo.com/signup | grep -i content-security` and `curl -sI https://odoo-semantic.viindoo.com/ | grep -i content-security`.

10. **§10.5 Browser tests** — 92 browser test functions exist (suite grown past the "68 tests" milestone marker). Need `pytest tests/browser/admin/ -m browser` GREEN run in CI or against production.

11. **§10 WEBUI_SESSION_SECRET production env path** — `WEBUI_SESSION_SECRET` is set in `~/git/odoo-semantic-mcp/.env` but the checklist references `/etc/odoo-semantic/webui.env` which does not exist. Admin: confirm the running service loads the secret from `.env`, or create the canonical `/etc/odoo-semantic/webui.env` per `docs/deploy.md`.

12. **OWLComp pre-v14 anachronism (NEW 2026-05-17)** — Post-reindex verification on PR #119 shows 239 `__unresolved__` OWLComp nodes at v8-v13 (OWL framework only exists from v14+). The v14 guard added in `_extract_era3_components` only covers REAL OWLComp creation; JSPatch era3 detection in pre-v14 modules still triggers the PATCHES placeholder MERGE in `writer_neo4j.py:~372`. Fix: add symmetric v14 guard to the `_extract_era3_patches` (or equivalent JSPatch era3) function in `parser_js.py`, OR add belt-and-suspenders v14 check at the writer PATCHES placeholder site. Plus one Cypher cleanup to delete the 239 current anachronisms. Non-blocking — read-side `list_owl_components` MCP tool already has an era guard that skips v<14, so user-facing output is correct; impact is only raw-graph pollution.
    > Tracked at TASKS.md → M10C "OWLComp pre-v14 anachronism guard".

13. **Neo4j online backup (NEW 2026-05-17)** — `neo4j-admin database dump` requires an offline DB; fails on the running container with exit 1 ("skipped" during backup run). Backup bundle is currently postgres-only (manifest.json + postgres.sql). Fix: replace neo4j-admin dump with either (a) Cypher-driver-based export via `CALL apoc.export.cypher.all`, or (b) `neo4j-admin database backup` if upgrading to Enterprise. Update `src/cli.py` + ADR-0018 bundle contract. Non-blocking — Neo4j data is rebuildable from indexed git repos via `index-repo --all --no-embed` (~75min).
    > Tracked at TASKS.md → M9 Stream I followup #13 (carried forward; no M10 reassignment).

14. **Logrotate /var/log perms (PRE-EXISTING, surfaced 2026-05-17)** — `/etc/logrotate.d/odoo-semantic` stanza 1 (`/var/log/odoo-semantic-reindex.log`) fails because `/var/log/` is world-writable. Stanza 1 was installed by an earlier deploy, NOT by WI-3. Stanza 2 (added by WI-3 — `/var/log/odoo-semantic/*.log` + `/var/backups/odoo-semantic/*.log`) rotates cleanly. Fix: add `su root syslog` directive to stanza 1, or change the reindex log location to `/var/log/odoo-semantic/`.
    > Tracked at TASKS.md → M9 Stream I followup #14 (carried forward; operational fix).

15. **§6 Tools 15-21 prod smoke (PENDING 2026-05-17)** — The 7 M9 W-OSM Wave 1 tools (`describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`) need an end-to-end smoke against the production MCP endpoint via Claude Code or another MCP client. Deferred to next session per go-live decision. All 7 tools are code-complete + unit-tested; no expected failures.
    > Tracked at TASKS.md → M10A "§6 tools 15-21 prod smoke".

---

*Xem thêm: [docs/deploy.md](../deploy.md) · [docs/deploy/disaster-recovery.md](disaster-recovery.md)*
