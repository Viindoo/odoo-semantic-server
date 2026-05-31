# Odoo Semantic MCP

> **Knowledge engine** hiểu sâu codebase Odoo — inheritance chain, view structure, JS patch —  
> expose qua MCP protocol để mọi AI coding tool đều dùng được.

---

## Vấn Đề Đang Giải Quyết

Khi AI coding tool (Claude Code, Codex, Gemini) làm việc với Odoo, chúng thường:

- Hallucinate tên field, method không tồn tại
- Không biết model `sale.order` được extend bởi bao nhiêu module
- Không trace được XPath override chain của một view
- Không biết đổi field `amount_total` sẽ ảnh hưởng đến những gì

**Odoo Semantic MCP** giải quyết điều này bằng cách index toàn bộ codebase Odoo (cross-repo, cross-version) vào Graph DB + Vector Store, rồi expose qua MCP server để AI tool query được.

---

## Cách Hoạt Động

```
Odoo repos (~/git/*_17.0/)
        │
        ▼  index một lần trên server
┌──────────────────────────────────────────────┐
│  Indexer Pipeline                            │
│  Neo4j + pgvector                            │
│                                              │
│  FastAPI JSON API  (port 8003)               │
│  Astro SSR + React islands  (port 4321)      │
│  MCP Server  (port 8002)                     │
└─────────────────────┬────────────────────────┘
                      │ nginx routes:
                      │  /api/*   → 8003 (JSON only)
                      │  /admin/* → 4321 (Astro SSR, auth-gated)
                      │  /        → 4321 (landing + hero)
                      │  /mcp     → 8002 (MCP protocol)
                      ▼
  Claude Code / VS Code / Codex / Gemini
  (user chỉ cần thêm URL vào config — không cài gì)
```

MCP server expose **24 tools** (4 ORM-validation tools added in v0.8 / M10.5 Phase 2; current surface unchanged at v0.11.0):

- **10 core tools (M1–M5):** `find_examples`, `impact_analysis`, `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point`
- **1 module overview tool (M9 Wave 1):** `describe_module`
- **3 superset discriminator tools (M11 Wave D — ADR-0028):** `model_inspect`, `module_inspect`, `entity_lookup` — route to the right flat tool by kind/entity-type, with structured `discriminator` in `structuredContent`
- **4 session tools (M11 Wave E — ADR-0029):** `set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles` — sticky per-API-key context (24h TTL) eliminates `odoo_version` repetition
- **2 stylesheet tools (M10A — ADR-0025):** `resolve_stylesheet`, `find_style_override` — CSS/SCSS chain + variable tracing across the indexed stylesheet graph
- **4 ORM-validation tools (M10.5 Phase 2 — v0.8):** `resolve_orm_chain`, `validate_domain`, `validate_depends`, `validate_relation` — static ORM checks (dotted-path resolution, domain field + version-aware operator validity, `@api.depends` paths, relation comodel) against the indexed graph before an AI client suggests a domain/depends/relation

Capabilities: Odoo core API lifecycle awareness + curated pattern catalogue + EE confusion guard + module architecture overview + entity enumeration (fields/methods/views) + UI-layer inventory (OWL components, QWeb templates, JS patches) + CSS/SCSS stylesheet indexing + static ORM validation (domain / @api.depends / relation / dotted-path chain) across v8 → v19 (v18 indexer-ready; v20 not yet released by Odoo).

MCP server also exposes **7 Resources** (`odoo://` URI scheme — M11 Wave F, ADR-0030) for bookmark-stable entity reads:

| URI template | Content | MIME |
|---|---|---|
| `odoo://{version}/model/{name}` | Markdown tree (same as `resolve_model`) | `text/markdown` |
| `odoo://{version}/field/{model}/{field}` | Markdown tree (same as `resolve_field`) | `text/markdown` |
| `odoo://{version}/method/{model}/{method}` | Markdown tree (same as `resolve_method`) | `text/markdown` |
| `odoo://{version}/view/{xmlid}` | Markdown tree (same as `resolve_view`) | `text/markdown` |
| `odoo://{version}/module/{name}` | Markdown tree (same as `describe_module`) | `text/markdown` |
| `odoo://{version}/pattern/{pattern_id}` | Curated pattern snippet + gotchas | `text/markdown` |
| `odoo://{version}/stylesheet/{module}/{file_path*}` | Raw CSS/SCSS source | `text/css` / `text/x-scss` |

The `{version}` segment accepts sentinels (`auto`, `default`, `latest`) that resolve to the API key's active version (set via `set_active_version`). Resource bodies are cached (LRU 1000 entries / 300s TTL, per-resolved-version so different active-version tenants never share a cache entry).

Indexer also covers **CSS/SCSS files** (M9 Coverage Fill): `:Stylesheet` Neo4j nodes with composite key `(file_path, module, odoo_version)` (paths stored repo-relative per ADR-0037 D1; `repo_id` property scopes `:IMPORTS` edges to prevent cross-repo collisions — ADR-0037 D8), `IMPORTS` edge chain for SCSS `@import` resolution, and pgvector semantic chunks (selector groups, variable definitions, media queries, mixin definitions) for stylesheet override analysis + branding/theme discovery.

→ [MCP tool routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/reference/mcp-tool-routing.md) cho routing matrix đầy đủ.

---

## Onboard End User (Zero Install)

Người dùng **không cài gì**. Nhận URL + API key từ admin → chọn AI tool:

> 🚀 **Nhanh nhất:** truy cập **https://odoo-semantic.viindoo.com/install/**, dán API key vào, copy snippet cho tool của bạn.

→ **[Client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/setup.md)** cho config từng client: Claude Code, Codex CLI, Gemini CLI, VS Code, Antigravity (snippets + pitfalls đầy đủ).

### Quick install — Claude Code

Hai plugin miễn phí (MIT): `odoo-semantic-mcp` (MCP config) + `odoo-semantic-skills` (26 skills, 3 agents, 9 personas). Cài skills tự kéo theo mcp:

```bash
claude plugin marketplace add Viindoo/claude-plugins --scope user
claude plugin install odoo-semantic-skills@viindoo-plugins --scope user   # auto-pulls odoo-semantic-mcp
```

Sau đó trong Claude Code session: `/odoo-semantic-mcp:connect` để nhập URL + API key.

> Chỉ muốn MCP? `claude plugin install odoo-semantic-mcp@viindoo-plugins --scope user` rồi `/odoo-semantic-mcp:connect`.
> Self-hosted hoặc không dùng plugin? Xem [manual MCP setup](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/setup.md#manual-mcp-setup-advanced--self-hosted) cho `claude mcp add` flow + pitfalls.

---

## Verify After Install — Natural-Language Prompts

Sau khi add xong, gõ prompt tự nhiên để verify agent pick MCP `odoo-semantic` đúng:
- *"Dùng odoo-semantic, liệt kê inheritance chain của `sale.order` trên Odoo 17.0."*

→ **[Client setup — Verify After Install](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/setup.md#verify-after-install)** cho prompt đầy đủ EN+VI + tín hiệu đúng/sai.

---

## Local E2E Quickstart

Test MCP local với Claude Code (không cần production server) — 5 phút setup.

→ **[`CONTRIBUTING.md §Local E2E`](CONTRIBUTING.md#local-e2e-test-mcp-local-trước-khi-production)** — Clone + install + index 1 repo + start server + config Claude Code.

---

## System Requirements (Server)

Sizing matrix (Minimum 2 vCPU/8GB cho M1–M2, Recommended 4 vCPU/16GB cho M1–M5 đầy đủ). M9 requires Node.js 22+ (pnpm 10+) for Astro service.

→ **[`docs/deploy.md §0.5 System Requirements`](docs/deploy.md#05-system-requirements)** cho table chi tiết + scaling guidance.

---

## Deploy Server (Admin)

Happy-path cho M9 (3 services):

> **Note:** This is a private Viindoo repository — cloning requires org membership or a granted deploy key.

```bash
git clone https://github.com/Viindoo/odoo-semantic-server && cd odoo-semantic-server
make install && docker compose up -d
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate

# Build Astro frontend (requires Node.js 22+, pnpm 10+):
cd site && pnpm install --frozen-lockfile && pnpm build && cd ..
```

Sau đó: register profile, index repos, generate FERNET_KEY + API key, start 3 systemd services (MCP :8002, FastAPI :8003, Astro :4321).

→ **[`docs/deploy.md`](docs/deploy.md)** cho production setup (all-in-one vs split-tier, systemd, nginx, TLS, backup).

---

## Tài Liệu

| File | Nội dung |
|------|----------|
| [Client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/setup.md) | **End-user client setup** — Claude Code, Codex, Gemini, VS Code, Antigravity (snippets + pitfalls đầy đủ) |
| [`docs/deploy.md`](docs/deploy.md) | **Admin deploy guide** — DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
| [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | **Pre-launch signoff** — 10 mục verify + 20 MCP tool sign-off table + 7 MCP resource sign-off trước khi mở public |
| [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | **DR runbook** — backup frequency, restore order, step-by-step commands, RTO estimate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Bắt đầu ở đây nếu bạn là developer** — setup, chạy tests, Local E2E, workflow |
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: Graph schema, Indexer pipeline, MCP tools, lộ trình |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Hướng dẫn stack: tại sao mỗi công nghệ được chọn, cách dùng đúng, các bẫy cần tránh |
| [`TASKS.md`](TASKS.md) | Bảng theo dõi tiến độ — cập nhật liên tục khi implement |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — schema, policy, storage decisions |
| [MCP tool routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/reference/mcp-tool-routing.md) | MCP tool routing matrix — 24 tools, trigger conditions, persona mapping |

---

## Persona Guides

Different roles get the most value from different tools. Quick-start guides:

| Persona | Primary Tools | Guide |
|---------|--------------|-------|
| CEO / Manager | `impact_analysis`, `check_module_exists`, `find_deprecated_usage` | [→ CEO Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/personas/ceo.md) |
| Developer | `model_inspect`, `find_override_point`, `suggest_pattern`, `lint_check` | [→ Dev Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/personas/dev.md) |
| Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` | [→ Consultant Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/personas/consultant.md) |
| Marketer | `api_version_diff`, `find_examples` | [→ Marketer Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/personas/marketer.md) |
| Sales | `check_module_exists`, `find_examples`, `model_inspect` | [→ Sales Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/personas/sales.md) |

> **Claude Code users:** Install the two free MIT plugins — `claude plugin install odoo-semantic-skills@viindoo-plugins` (sau khi `claude plugin marketplace add Viindoo/claude-plugins`; skills tự kéo theo `odoo-semantic-mcp`), rồi `/odoo-semantic-mcp:connect`. Alternative: dùng [install page](https://odoo-semantic.viindoo.com/install/) → tab Claude Code → sub-tab "Plugin".
> **Gemini users:** See [Gem instructions](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/gemini-gem-instructions.md).
> **ChatGPT users:** See [Custom GPT instructions](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/openai-gpt-instructions.md).
> **Cursor users:** See [Cursor rules](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/cursor-rules.md).

---

## Trạng Thái Hiện Tại

**Latest release:** v0.13.1 (2026-05-28) — Self-host waitlist + post-v0.13.0 cleanup (PR #204). Adds `POST /api/waitlist` endpoint (5/min per-IP rate-limit, admin email notification), `src/web_ui/email.py` sender abstraction, migration `m13_008_waitlist_emails`. Tool count stays **24**. See CHANGELOG.md.

**v0.11.1 (2026-05-23)** — Pre-LIVE read-side hygiene + stylesheet/lint cleanup. See CHANGELOG.md.

**v0.11.0 (2026-05-23)** — Parser correctness wave WG-1..WG-5 (Python v8-v19, JS OWLComp+JSPatch, writer schema). See CHANGELOG.md.

**Web-UI multi-tenant RBAC + self-service portal (W0-W4, merged 2026-05-25):** Batch 5 wave hoàn thành giao diện web quản trị + tenant self-service. W0 (#174) — admin gate 19 route mutating + `SIGNUP_ENABLED` flag (default off). W1 (#177) — `tenant_members` (m13_005) + admin tenant CRUD + ADR-0038. W2 (#179) — customer self-service portal (`/account/repos`) + `tenant_write_allowed` write-side RBAC. W3 (#180) — diagnostics endpoint + admin user creation + audit-log viewer + audit coverage regression guard. W4 (#181) — `GET /api/versions` data-driven + 3 version dropdown + worker controls (`profile_workers`/`max_workers`/`--gc`). Tool count stays **24**; migration m13_005 required. See CHANGELOG.md `[Unreleased]`.

**Production deploy:** 2026-05-28 — PRs #200 + #204 deployed. Migrations m13_006 + m13_007 + m13_008 applied. All api_keys backfilled to free-grandfathered plan; osm_reader grants verified across new schema objects. All 3 services healthy (MCP :8002, FastAPI :8003, Astro :4321).

**Auth flow unification (feat/m10b-auth-unify, 2026-05-29):** `/login` now canonical (`/admin/login` 301→`/login`); OAuth Google/GitHub buttons on `/signup` (cookie `oauth_from` phân biệt origin); reset-password enforce `auth.password_min_length` (default 12) + common-pw blocklist (FE+BE) + TOCTOU `SELECT...FOR UPDATE` guard; shared `AuthLayout` + 22-item UX/a11y. OAuth paths `/admin/auth/*` giữ nguyên. Tool count stays **24**; no migration. Closes the prior customer-onboarding UI gaps (forgot-password e2e, `/pricing` nav, `/login` alias, OAuth error banner).

**Free-plan consolidation + auto-onboarding (fix/auth-ux-oauth-cache-plans, 2026-05-29):** Single unified `free` public plan replaces `free-grandfathered` (migration m13_013); all new signups (password + OAuth) auto-mint an API key on the free plan. SameSite cookie fix for Google sign-in (Strict→Lax), SSR cache-control + Clear-Site-Data on logout, role-aware post-login landing. Tool count stays **24**; migration m13_013 required.

**M10B P1 billing — engineering complete (feat/m10b-p1-billing, 2026-05-30):** Full billing
completion across 6 waves (W1-W6). Single migration **m13_014** covers the entire billing schema:
original P1 base (subscriptions + webhook ledger) **plus** all W1 schema hardening gộp vào:
`subscriptions.cancel_at_period_end` + `plans.prices` JSONB + guarded seed (formerly m13_015 draft),
`webui_users.terms_accepted_at` consent (formerly m13_016 draft), drop waitlist plan CHECK — now
DB-derived (formerly m13_017 draft). **Note:** file numbers m13_015 and m13_016 were subsequently
reused by PR #223 for new migrations (`plans.pricing_model` and `plans.min_seats` respectively) —
deploy must also run those two files after m13_014. Vendor-generic webhook pipeline (`WebhookAdapter` + `run_webhook_pipeline` in
`src/billing/webhook_pipeline.py`); `src/billing/_db.py` (`slug_to_plan_id`). Self-service
cancel-at-period-end: outbound Polar REST client (`src/billing/polar_api.py`, `POLAR_API_KEY`,
fail-closed); `POST /api/account/subscription/cancel` + `GET /api/account/subscription`.
Admin plan price editing (`PATCH /api/admin/plans/{slug}` now accepts `price_cents / currency /
billing_interval / trial_days / prices / is_archived`); 8 new `billing.*` settings (total 11
billing settings, 28 settings catalogue entries; includes `team_min_seats=3` **enforced**).
Legal pages `/terms` + `/refund` + `/privacy` (DRAFT badge — pending legal sign-off before
flipping `billing.paid_checkout_enabled`). Required signup consent checkbox + `terms_accepted_at`
recording. `/account/billing` dashboard page + `BillingDashboard` React island (status/renewal/
cancel state). `/pricing` data-driven (`prerender=false`) with live USD prices from `plans.prices`
(multi-currency display deferred to P2).
**Tool count stays 24** (all web/webhook/Astro only; no new MCP tools). See
[ADR-0039 Amendment — completion](docs/adr/0039-commercialization-platform.md) and CHANGELOG.md `[Unreleased]`.
**Pending (owner/legal):** legal DRAFT sign-off + `billing.paid_checkout_enabled` flip; Polar
KYB onboarding; confirm Polar cancel endpoint (`src/billing/polar_api.py` constants) + webhook
fields (`src/billing/polar.py`) against live Polar docs; register webhook URL + product→plan map
in Polar dashboard.

**Active work:** PR #223 (`feat/site-pricing-ux`) — per-seat pricing UX, `/tools` page, shared
SiteHeader/SiteFooter, `support.helpdesk_url` setting + `GET /api/site-config`, plugin content
split, billing provision race fix. Migrations: m13_015 (`plans.pricing_model`) + m13_016
(`plans.min_seats`). Tool count stays **24**.
**Deferred:** M10B P2 (multi-IdP "Viindoo Account", buyer≠user split, ERP/VAS adapter),
M10C nonce-CSP (blocked on Astro v5.1+), recall benchmark, §6 prod smoke 14 tools (deep),
VN persona docs.

**Next milestones (roadmap):**
- **M10B P1 "Commercialization Wow"** — M10B P0 (quota gating + plan schema + usage dashboard) shipped in v0.13.0. **P1 engineering-complete** (merged): Polar.sh webhook + Entitlement Activation API + claim-on-login + W1-W6 completion (vendor-generic pipeline, self-serve cancel, admin config, legal + consent, billing dashboard; tool count stays 24). Single migration **m13_014** covers all billing schema (cancel_at_period_end, prices JSONB, terms_accepted_at, waitlist CHECK drop — formerly separate drafts, now merged into m13_014). **Note:** m13_015 and m13_016 file numbers reused by PR #223 (pricing_model, min_seats). **Owner/legal sign-off pending** (legal DRAFT pages, `paid_checkout_enabled` flip, KYB, Polar endpoint confirmation). **P2 pending:** multi-IdP "Viindoo Account", buyer≠user split, ERP sale.order webhook + VAS. Architecture: [ADR-0039](docs/adr/0039-commercialization-platform.md).
- **M10B P1.5 "Admin Settings"** — Runtime configuration UI shipped (Unreleased). Ops
  tune RPM/quota/batch without SSH/redeploy. See [ADR-0042](docs/adr/0042-admin-settings-module.md).
  **fix/mfa-step-up-freshness** — Bug fix: fresh-MFA gate was permanently 403 because
  `mfa_verified_at` was never written. Fixed by writing the timestamp in `totp_login` and
  new `POST /api/auth/totp/step-up` endpoint; window runtime-configurable via
  `auth.mfa_freshness_seconds` (Tier-1 16th setting). See [ADR-0043](docs/adr/0043-mfa-step-up-freshness.md).
- **M10C remaining** — Prometheus `embedder_batch_duration_seconds` histogram, nonce-based CSP (blocked — awaits Astro v5.1+ nonce API).

→ [`TASKS.md`](TASKS.md) cho task chi tiết từng milestone. → [`CHANGELOG.md`](CHANGELOG.md) cho release notes.

### Admin Settings Module (ADR-0042 — Unreleased)

OSM v0.14.0 (upcoming) ship **Admin Settings** — web UI cho phép admin +
tenant_owner tinker 16 Tier-1 settings (auth + embedding + indexer + mcp,
incl. `auth.mfa_freshness_seconds` per ADR-0043) + 4 plan tier + 16 EE module
+ 115 pattern KHÔNG cần SSH/redeploy. Hot-reload ≤60s. Audit + rollback per
ADR-0021. Tenant `quota.*` override Phase 1.

Access: `/admin/settings` (admin) + `/tenant/settings` (tenant_admin role).

**Tool count stays 24** — Admin Settings is web-UI-only, no new MCP tools.

→ [`docs/adr/0042-admin-settings-module.md`](docs/adr/0042-admin-settings-module.md)

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Mở [`TASKS.md`](TASKS.md) — tìm milestone đầu tiên có `[ ]` hoặc `[~]`, đó là điểm vào
3. Nếu milestone đó có plan tương ứng — follow từng bước. Nếu chưa có plan, đề xuất plan trước khi code.
4. Tuân thủ hai nguyên tắc cốt lõi trong `CLAUDE.md` ở mọi quyết định
