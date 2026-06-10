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
                      │ nginx routes (actual prod):
                      │  /api/waitlist     → 8003 (separate rate pool)
                      │  /api/*            → 8003 (JSON only)
                      │  /mcp              → 8002 (MCP protocol)
                      │  /install/         → 8002 (MCP server)
                      │  /health           → 8002 (liveness — MCP, no DB I/O)
                      │  /ready            → 8002 (readiness probe, cached 60s — MCP)
                      │  /metrics          → 8002 (Prometheus — MCP, IP-restricted)
                      │  /.well-known/openid-configuration → nginx inline (no backend)
                      │  /                 → 4321 (Astro SSR, catch-all;
                      │                           routes /admin/* internally)
                      ▼
  Claude Code / VS Code / Codex / Gemini
  (user chỉ cần thêm URL vào config — không cài gì)
```

MCP server expose **25 tools** (4 ORM-validation tools added in v0.8 / M10.5 Phase 2; +1 `profile_inspect` in Wave 2 WI-4 #260):

- **10 core tools (M1–M5):** `find_examples`, `impact_analysis`, `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point`
- **1 module overview tool (M9 Wave 1):** `describe_module`
- **3 superset discriminator tools (M11 Wave D — ADR-0028):** `model_inspect`, `module_inspect`, `entity_lookup` — route to the right flat tool by kind/entity-type; uniform raw-text output (WI-5 #261/#265: `output_schema=None` on all tools, no `structuredContent` wrap)
- **4 session tools (M11 Wave E - ADR-0029):** `set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles` - sticky context per live MCP session (keyed by `mcp-session-id`; single api-key/`_nosession` fallback for stdio/header-less callers), in-memory with a 24h idle TTL, resets on server restart; eliminates `odoo_version` repetition
- **1 profile introspection tool (Wave 2 WI-4 — ADR-0028, #260, #259):** `profile_inspect` — profile-level discriminator: summary (ancestor chain + children + repos + module_count), repos (deduped across ancestor chain), modules (paginated module list scoped to profile). Closes the introspection gap: "which repos/modules make up profile X?" now answerable in <=2 calls.
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

Hai plugin miễn phí (MIT): `odoo-semantic-mcp` (MCP config) + `odoo-semantic-skills` (26 skills, 3 agents, 9 personas, tùy chọn). Bắt đầu với plugin MCP:

```bash
claude plugin marketplace add Viindoo/claude-plugins --scope user
claude plugin install odoo-semantic-mcp@viindoo-plugins --scope user
```

Sau đó trong Claude Code session: `/odoo-semantic-mcp:connect` để nhập URL + API key.

> Muốn thêm skills, agents & personas? Cài thêm: `claude plugin install odoo-semantic-skills@viindoo-plugins --scope user` (tự kéo theo `odoo-semantic-mcp` nếu bạn bỏ qua bước trên).
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
| [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | **Pre-launch signoff** — 10 mục verify + 25 MCP tool sign-off table + 7 MCP resource sign-off trước khi mở public |
| [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | **DR runbook** — backup frequency, restore order, step-by-step commands, RTO estimate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Bắt đầu ở đây nếu bạn là developer** — setup, chạy tests, Local E2E, workflow |
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: Graph schema, Indexer pipeline, MCP tools, lộ trình |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Hướng dẫn stack: tại sao mỗi công nghệ được chọn, cách dùng đúng, các bẫy cần tránh |
| [`TASKS.md`](TASKS.md) | Bảng theo dõi tiến độ — cập nhật liên tục khi implement |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — schema, policy, storage decisions |
| [MCP tool routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/docs/reference/mcp-tool-routing.md) | MCP tool routing matrix — 25 tools, trigger conditions, persona mapping |

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

> **Claude Code users:** Thêm marketplace rồi cài plugin MCP — `claude plugin marketplace add Viindoo/claude-plugins --scope user` rồi `claude plugin install odoo-semantic-mcp@viindoo-plugins --scope user`, sau đó `/odoo-semantic-mcp:connect`. Tùy chọn: cài thêm `odoo-semantic-skills@viindoo-plugins` để có skills + agents + personas. Hoặc dùng [install page](https://odoo-semantic.viindoo.com/install/) → tab Claude Code → sub-tab "Plugin".
> **Gemini users:** See [Gem instructions](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/gemini-gem-instructions.md).
> **ChatGPT users:** See [Custom GPT instructions](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/openai-gpt-instructions.md).
> **Cursor users:** See [Cursor rules](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-semantic-skills/snippets/cursor-rules.md).

---

## Trạng Thái Hiện Tại

**Active work — ORM hang + lint false-green fix wave (fix/#271-#273, ADR-0048):** Two production
issues fixed in one wave. (1) Four ORM tools (`resolve_orm_chain` / `validate_domain` /
`validate_depends` / `validate_relation`) hung indefinitely on dense inheritance graphs (11 zombie
transactions, 19-24h on prod) — fixed by per-hop name-dedup read query + 30s driver timeout +
semaphore (8 slots). (2) `lint_check` false-green on SQL injection — fixed by `code_pattern` regex
data + pattern-first hybrid matcher (V0.5). Writer same-name INHERITS topology changed to K×D
(extender→definition only). Tool count stays **25**. No Postgres migration. Cleanup script
`ops/cleanup_same_name_inherits_mesh.cypher` must be run off-peak after deploy (backup ADR-0018
required first). See [ADR-0048](docs/adr/0048-inherits-topology-and-orm-read-bounds.md) and
CHANGELOG.md `[Unreleased]`.

**Latest release:** v0.13.1 (2026-05-28) — Self-host waitlist + post-v0.13.0 cleanup (PR #204). Adds `POST /api/waitlist` endpoint (5/min per-IP rate-limit, admin email notification), `src/web_ui/email.py` sender abstraction, migration `m13_008_waitlist_emails`. Tool count stays **24**. See CHANGELOG.md.

**v0.11.1 (2026-05-23)** — Pre-LIVE read-side hygiene + stylesheet/lint cleanup. See CHANGELOG.md.

**v0.11.0 (2026-05-23)** — Parser correctness wave WG-1..WG-5 (Python v8-v19, JS OWLComp+JSPatch, writer schema). See CHANGELOG.md.

**Web-UI multi-tenant RBAC + self-service portal (W0-W4, merged 2026-05-25):** Batch 5 wave hoàn thành giao diện web quản trị + tenant self-service. W0 (#174) — admin gate 19 route mutating + `SIGNUP_ENABLED` flag (default off). W1 (#177) — `tenant_members` (m13_005) + admin tenant CRUD + ADR-0038. W2 (#179) — customer self-service portal (`/account/repos`) + `tenant_write_allowed` write-side RBAC. W3 (#180) — diagnostics endpoint + admin user creation + audit-log viewer + audit coverage regression guard. W4 (#181) — `GET /api/versions` data-driven + 3 version dropdown + worker controls (`profile_workers`/`max_workers`/`--gc`). Tool count stays **24**; migration m13_005 required. See CHANGELOG.md `[Merged into v0.13.1]`.

**Production deploy:** 2026-06-06 — HEAD 127ce83 (PR #269); all migrations through m13_021 applied. All api_keys backfilled to free-grandfathered plan; osm_reader grants verified across new schema objects. All 3 services healthy (MCP :8002, FastAPI :8003, Astro :4321).

**Auth flow unification (feat/m10b-auth-unify, 2026-05-29):** `/login` now canonical (`/admin/login` 301→`/login`); OAuth Google/GitHub buttons on `/signup` (cookie `oauth_from` phân biệt origin); reset-password enforce `auth.password_min_length` (default 12) + common-pw blocklist (FE+BE) + TOCTOU `SELECT...FOR UPDATE` guard; shared `AuthLayout` + 22-item UX/a11y. OAuth paths `/admin/auth/*` giữ nguyên. Tool count stays **24**; no migration. Closes the prior customer-onboarding UI gaps (forgot-password e2e, `/pricing` nav, `/login` alias, OAuth error banner).

**Free-plan consolidation + auto-onboarding (fix/auth-ux-oauth-cache-plans, 2026-05-29):** Single unified `free` public plan replaces `free-grandfathered` (migration m13_013); all new signups (password + OAuth) auto-mint an API key on the free plan. SameSite cookie fix for Google sign-in (Strict→Lax), SSR cache-control + Clear-Site-Data on logout, role-aware post-login landing. Tool count stays **24**; migration m13_013 required.

**Auto-minted key revealed once after signup (PR #256, 2026-06-04):** The free-plan key auto-minted on first signup is now surfaced once for copy in the existing copy-once banner on `/account/api-keys`, closing the gap where the plaintext was discarded at the mint call sites and users had to manually create a second key. `POST /api/auth/verify-email` and `POST /api/auth/oauth-login` now return the plaintext as `new_api_key` (`null` for returning users / already-keyed users / mint failure). The password flow carries it via `sessionStorage['osm-new-api-key']`; the OAuth server-side 302 via a short-lived JS-readable cookie `osm_new_key` (`Path=/account/api-keys`, `Max-Age=60`, **`SameSite=Lax` — NOT Strict**, because Strict is dropped on the OAuth redirect hop, same reason as the session cookie). New non-admin OAuth signups are routed to `/account/api-keys` so a deep-link `?return=` cannot strand the one-time key; the reveal consumes both carriers once and only displays `osm_`-prefixed values. Lazy-mint `GET /api/api-keys` stays metadata-only by design (an idempotent GET must not surface a one-time secret); plaintext is never persisted server-side. Web/Astro only — tool count stays **24**; no migration.

**M10B P1 billing — engineering complete (feat/m10b-p1-billing, 2026-05-30):** Full billing
completion across 6 waves (W1-W6). **Fresh-install operators must apply migrations in order:
m13_014 → m13_015 → m13_016 → m13_017 → (m13_018).** Each is a distinct `.sql` file:
m13_014 = P1 base billing schema (subscriptions + webhook ledger + `cancel_at_period_end` +
`plans.prices` JSONB + guarded seed + `terms_accepted_at` + waitlist CHECK drop);
m13_015 = `plans.pricing_model` (PR #223); m13_016 = `plans.min_seats` (PR #223);
m13_017 = CRD withdrawal consent — `subscriptions.buyer_type` + `withdrawal_waiver_accepted_at` (PR #224);
m13_018 = embedding provider columns — `embedding_model` + `embedding_dim` (PR #228).
(m13_015 and m13_016 file numbers were reused by PR #223 after earlier drafts were folded into m13_014;
m13_017 file number reused by PR #224 — see PR #224 entry below.) Vendor-generic webhook pipeline (`WebhookAdapter` + `run_webhook_pipeline` in
`src/billing/webhook_pipeline.py`); `src/billing/_db.py` (`slug_to_plan_id`). Self-service
cancel-at-period-end: outbound Polar REST client (`src/billing/polar_api.py`, `POLAR_API_KEY`,
fail-closed); `POST /api/account/subscription/cancel` + `GET /api/account/subscription`.
Admin plan price editing (`PATCH /api/admin/plans/{slug}` now accepts `price_cents / currency /
billing_interval / trial_days / prices / is_archived`); 8 new `billing.*` settings (total 11
billing settings, 29 settings catalogue entries per `src/settings_registry.py`; includes `team_min_seats=3` **enforced**).
Legal pages `/terms` + `/refund` + `/privacy` (DRAFT badge removed — CEO sign-off 2026-06-01, PR #224; external counsel pass recommended post-launch). Required signup consent checkbox + `terms_accepted_at`
recording. `/account/billing` dashboard page + `BillingDashboard` React island (status/renewal/
cancel state). `/pricing` data-driven (`prerender=false`) with live USD prices from `plans.prices`
(multi-currency display deferred to P2).
**Tool count stays 25** (all web/webhook/Astro only; no new MCP tools). See
[ADR-0039 Amendment — completion](docs/adr/0039-commercialization-platform.md) and CHANGELOG.md `[Unreleased]`.
**Live in prod:** `billing.paid_checkout_enabled=true` (verified in DB + `/api/site-config`).
**Still pending:** Polar KYB onboarding; confirm Polar cancel endpoint (`src/billing/polar_api.py` constants) + webhook
fields (`src/billing/polar.py`) against live Polar docs; register webhook URL + product→plan map
in Polar dashboard.

**Embedding infrastructure wave (wave/wi-f, PR #228 — fix #226 + #227 + provider decoupling):**
Token-bounded chunking + provider abstraction + MCP anti-hang. Three concerns addressed:

1. **Fix #226 — token-bounded chunking (ADR-0044):** chunking layer (`_sliding`, pattern/view/JS/style
   helpers) now enforces `EMBEDDER_TOKEN_BUDGET` (3500 tokens) via `estimate_tokens` /
   `split_by_token_budget` helpers. MCP query strings capped at the same budget. Truncation
   choke-point in `_BaseHttpEmbedder` as last defence. Bug B length-guard in `_embed_one`. Resilient
   skip-log in `_embed_chunks_resilient` — single bad chunk cannot abort full module write.
2. **Provider abstraction (ADR-0045):** `EmbedderClient` Protocol + `_BaseHttpEmbedder` shared base +
   `OpenAICompatEmbedder` (OpenAI / Voyage / TEI / vLLM / LiteLLM) + `make_embedder()` factory
   (select backend via `EMBEDDER_BACKEND` env). `embedding_model` + `embedding_dim` columns
   (migration **m13_018**) stamp every vector row; fail-fast `EmbedderDimMismatch` guard prevents
   silent vector-space corruption on provider switch.
3. **Fix #227 — MCP embed concurrency / anti-hang (ADR-0046):** root cause was FastMCP calling
   `sync def` tool handlers on the event loop thread — one blocking embed froze all requests for ~11h
   (production). Fix: async hot path (`embed_async` via `asyncio.to_thread`), 30s query timeout vs.
   1200s batch timeout, `asyncio.Semaphore(EMBEDDER_MAX_CONCURRENCY)` cap, `EmbedOverloaded`
   fast-reject in 5s, uvicorn `limit_concurrency` backpressure. `/health` is now a pure liveness
   probe (no DB I/O); `/ready` is a new HTTP readiness endpoint (cached 60s, NOT an MCP tool).

Tool count stays **25**. Migration **m13_018** was required for this wave (after m13_017); current prod migration level is m13_021.
See [ADR-0044](docs/adr/0044-token-bounded-embedding.md), [ADR-0045](docs/adr/0045-embedding-provider-abstraction.md), [ADR-0046](docs/adr/0046-mcp-embed-concurrency-anti-hang.md).

**Active work / recently merged:** PR #232 (`feat/landing-living-cartography`) — landing redesign + /examples cartography page + docs cleanup (HEAD aa29422). Prior merged work now in prod: PR #225 (`analytics.ga_measurement_id` app_setting + GA snippet injection); PR #228 (`wave/wi-f` — token-bounded embedding ADR-0044, provider abstraction ADR-0045, MCP anti-hang ADR-0046, migration m13_018); PR #229 (readiness probe `/ready` + `/health` pure liveness split); PR #224 (`feat/launch-prep` — install MCP-first, brand SSOT, SEO, legal pages, CRD consent, migration m13_017). Tool count stays **25**.
**Deferred:** M10B P2 (multi-IdP "Viindoo Account", buyer≠user split, ERP/VAS adapter),
M10C nonce-CSP (blocked on Astro v5.1+), recall benchmark, §6 prod smoke 14 tools (deep),
VN persona docs.

**Next milestones (roadmap):**
- **M10B P1 "Commercialization Wow"** — M10B P0 (quota gating + plan schema + usage dashboard) shipped in v0.13.0. **P1 engineering-complete** (merged): Polar.sh webhook + Entitlement Activation API + claim-on-login + W1-W6 completion (vendor-generic pipeline, self-serve cancel, admin config, legal + consent, billing dashboard; tool count stays 24). **Deploy order: m13_014 → m13_015 → m13_016 → m13_017 → (m13_018)** — four distinct billing migrations plus the embedding-provider migration. m13_014 = P1 base billing schema; m13_015/016 = pricing_model + min_seats (PR #223); m13_017 = CRD withdrawal consent (PR #224); m13_018 = embedding provider columns (PR #228). (m13_015/016/017 file numbers were reused after earlier drafts were folded into m13_014.) **Legal CEO sign-off done (PR #224, DRAFT removed 2026-06-01). Live in prod: `billing.paid_checkout_enabled=true`. Still pending:** Polar KYB onboarding + Polar cancel-endpoint/webhook-field confirmation + webhook URL + product→plan map registration in Polar dashboard. **P2 pending:** multi-IdP "Viindoo Account", buyer≠user split, ERP sale.order webhook + VAS. Architecture: [ADR-0039](docs/adr/0039-commercialization-platform.md).
- **M10B P1.5 "Admin Settings"** — Runtime configuration UI shipped and live in prod (pending v0.14.0 tag). Ops
  tune RPM/quota/batch without SSH/redeploy. See [ADR-0042](docs/adr/0042-admin-settings-module.md).
  **fix/mfa-step-up-freshness** — Bug fix: fresh-MFA gate was permanently 403 because
  `mfa_verified_at` was never written. Fixed by writing the timestamp in `totp_login` and
  new `POST /api/auth/totp/step-up` endpoint; window runtime-configurable via
  `auth.mfa_freshness_seconds` (Tier-1 16th setting). See [ADR-0043](docs/adr/0043-mfa-step-up-freshness.md).
- **M10C remaining** — Prometheus `embedder_batch_duration_seconds` histogram, nonce-based CSP (blocked — awaits Astro v5.1+ nonce API).

→ [`TASKS.md`](TASKS.md) cho task chi tiết từng milestone. → [`CHANGELOG.md`](CHANGELOG.md) cho release notes.

### Admin Settings Module (ADR-0042 — Deployed, pending v0.14.0 tag)

**Admin Settings** has shipped and is live in prod ahead of a formal v0.14.0 tag — web UI cho phép admin +
tenant_owner tinker 18 Tier-1 settings (auth + quota + embedding + indexer + mcp + support + analytics,
incl. `auth.mfa_freshness_seconds` per ADR-0043, `support.helpdesk_url` PR #223,
`analytics.ga_measurement_id` PR #225) + 4 plan tier + 16 EE module
+ 115 pattern KHÔNG cần SSH/redeploy. Hot-reload ≤60s. Audit + rollback per
ADR-0021. Tenant `quota.*` override Phase 1.

Access: `/admin/settings` (admin) + `/tenant/settings` (tenant_admin role).

**Tool count stays 25** — Admin Settings is web-UI-only, no new MCP tools.

→ [`docs/adr/0042-admin-settings-module.md`](docs/adr/0042-admin-settings-module.md)

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Mở [`TASKS.md`](TASKS.md) — tìm milestone đầu tiên có `[ ]` hoặc `[~]`, đó là điểm vào
3. Nếu milestone đó có plan tương ứng — follow từng bước. Nếu chưa có plan, đề xuất plan trước khi code.
4. Tuân thủ hai nguyên tắc cốt lõi trong `CLAUDE.md` ở mọi quyết định
