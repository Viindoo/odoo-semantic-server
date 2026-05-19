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

MCP server expose **21 tools** (M1–M5 + M9 Wave 1): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `find_examples`, `impact_analysis`, `lookup_core_api`, `api_version_diff`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point`, `describe_module`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches` — Odoo core API lifecycle awareness + curated pattern catalogue + EE confusion guard + module architecture overview + entity enumeration (fields/methods/views) + UI-layer inventory (OWL components, QWeb templates, JS patches) + CSS/SCSS stylesheet indexing across v8 → v17, v19 (v18 pending — see OBS-1; v20 not yet released by Odoo).

Indexer also covers **CSS/SCSS files** (M9 Coverage Fill): `:Stylesheet` Neo4j nodes with composite key `(file_path, module, odoo_version)`, `IMPORTS` edge chain for SCSS `@import` resolution, and pgvector semantic chunks (selector groups, variable definitions, media queries, mixin definitions) for stylesheet override analysis + branding/theme discovery.

→ [`docs/reference/mcp-tool-routing.md`](docs/reference/mcp-tool-routing.md) cho routing matrix đầy đủ.

---

## Onboard End User (Zero Install)

Người dùng **không cài gì**. Nhận URL + API key từ admin → chọn AI tool:

> 🚀 **Nhanh nhất:** truy cập **https://odoo-semantic.viindoo.com/install/**, dán API key vào, copy snippet cho tool của bạn.

→ **[`docs/client-setup.md`](docs/client-setup.md)** cho config từng client: Claude Code, Codex CLI, Gemini CLI, VS Code, Antigravity (snippets + pitfalls đầy đủ).

### Quick install — Claude Code

```bash
claude plugin marketplace add Viindoo/claude-plugins --scope user
claude plugin install odoo-semantic@viindoo-plugins --scope user
```

Sau đó trong Claude Code session: `/odoo-semantic:connect` để nhập URL + API key.

> Self-hosted hoặc không dùng plugin? Xem [manual MCP setup](docs/client-setup.md#manual-mcp-setup-advanced--self-hosted) cho `claude mcp add` flow + pitfalls.

---

## Verify After Install — Natural-Language Prompts

Sau khi add xong, gõ prompt tự nhiên để verify agent pick MCP `odoo-semantic` đúng:
- *"Dùng odoo-semantic, liệt kê inheritance chain của `sale.order` trên Odoo 17.0."*

→ **[`docs/client-setup.md#verify-after-install`](docs/client-setup.md#verify-after-install)** cho prompt đầy đủ EN+VI + tín hiệu đúng/sai.

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

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp && cd odoo-semantic-mcp
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
| [`docs/client-setup.md`](docs/client-setup.md) | **End-user client setup** — Claude Code, Codex, Gemini, VS Code, Antigravity (snippets + pitfalls đầy đủ) |
| [`docs/deploy.md`](docs/deploy.md) | **Admin deploy guide** — DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
| [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | **Pre-launch signoff** — 10 mục verify + 21 MCP tool sign-off table trước khi mở public |
| [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | **DR runbook** — backup frequency, restore order, step-by-step commands, RTO estimate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **Bắt đầu ở đây nếu bạn là developer** — setup, chạy tests, Local E2E, workflow |
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Thiết kế kiến trúc đầy đủ: Graph schema, Indexer pipeline, MCP tools, lộ trình |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Hướng dẫn stack: tại sao mỗi công nghệ được chọn, cách dùng đúng, các bẫy cần tránh |
| [`TASKS.md`](TASKS.md) | Bảng theo dõi tiến độ — cập nhật liên tục khi implement |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — schema, policy, storage decisions |
| [`docs/reference/mcp-tool-routing.md`](docs/reference/mcp-tool-routing.md) | MCP tool routing matrix — 21 tools, trigger conditions, persona mapping |

---

## Persona Guides

Different roles get the most value from different tools. Quick-start guides:

| Persona | Primary Tools | Guide |
|---------|--------------|-------|
| CEO / Manager | `impact_analysis`, `check_module_exists`, `find_deprecated_usage` | [→ CEO Guide](docs/personas/ceo.md) |
| Developer | `resolve_model`, `find_override_point`, `suggest_pattern`, `lint_check` | [→ Dev Guide](docs/personas/dev.md) |
| Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` | [→ Consultant Guide](docs/personas/consultant.md) |
| Marketer | `api_version_diff`, `find_examples` | [→ Marketer Guide](docs/personas/marketer.md) |
| Sales | `check_module_exists`, `find_examples`, `resolve_model` | [→ Sales Guide](docs/personas/sales.md) |

> **Claude Code users:** Install the Odoo Semantic plugin — `claude plugin install odoo-semantic@viindoo-plugins` (sau khi `claude plugin marketplace add Viindoo/claude-plugins`), rồi `/odoo-semantic:connect`. Alternative: dùng [install page](https://odoo-semantic.viindoo.com/install/) → tab Claude Code → sub-tab "Plugin".
> **Gemini users:** See [Gem instructions](dist/gemini-gem-instructions.md).
> **ChatGPT users:** See [Custom GPT instructions](dist/openai-gpt-instructions.md).
> **Cursor users:** See [Cursor rules](dist/cursor-rules.md).

---

## Trạng Thái Hiện Tại

**Latest release:** v0.4.1 (2026-05-16) — M9 follow-up complete. Web UI parity for repo & profile management.

**Production deploy:** 2026-05-17 — PR #119 go-live batch deployed (writer profile stub fix eliminating 5,988 NULL nodes, MFA flag sync, backup CLI docker-exec fallback + nightly systemd timer, `/api/health` auth-exempt endpoint, ADR-0016 D7 stub policy). PR #117 (migration 0004 self-contained SQL rescue) + PR #118 (CSP + Permissions-Policy headers) also live. Admin-invite signup model active. See [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) for signoff state.

**Active work:** M9 Coverage Fill batch (PR #120 merged 2026-05-17, pending prod deploy) — CSS/SCSS parser, v8 era1 field gap fix, PatternExample v9-v15, LintRule/CLIFlag static curation v8-v19. Plus go-live followups: OWLComp v14 guard for JSPatch era3 (239 anachronistic stubs), Neo4j online backup (replace neo4j-admin dump with Cypher export), §6 tools 15-21 prod smoke (deferred next session).

**Next milestones (roadmap):**
- **M10 "Billing Wow"** — Stripe subscription + plan tiers + coverage-fill follow-ups: MCP Stylesheet tools (`resolve_stylesheet`, `find_style_override`), Prometheus `embedder_batch_duration_seconds` metric, M10 Quick Wins (magic fields, `from_module` param, `noqa` support, CLI batch audit), nonce-based CSP.
- **M10.5 "ORM Intelligence Wow"** — 4 new MCP tools (`validate_domain`, `resolve_orm_chain`, `validate_depends`, `validate_relation`) for static ORM validation before AI client suggests a domain/depends.
- **M11 "Architectural Wow"** — discriminator consolidation: `model_inspect`/`module_inspect`/`entity_lookup` supersets replace 10 flat tools with 1-major-release deprecation timeline (ADR-0028); implicit session context: per-API-key sticky `odoo_version`+`profile_name` with 24h TTL + sentinel defense (ADR-0029); parser hooks `(min_version, max_version, fn)` registry refactor (supersedes parts of ADR-0005), RelaxNG XML schema validation port from Odoo LS, pattern catalogue expansion 35 → 100+, lint rules curation 10-30 → 50+/version.

→ [`TASKS.md`](TASKS.md) cho task chi tiết từng milestone. → [`CHANGELOG.md`](CHANGELOG.md) cho release notes.

---

## Cho AI Agent

Nếu bạn là AI agent và cần bắt đầu implement:

1. Đọc [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) — hiểu toàn bộ kiến trúc
2. Mở [`TASKS.md`](TASKS.md) — tìm milestone đầu tiên có `[ ]` hoặc `[~]`, đó là điểm vào
3. Nếu milestone đó có plan tương ứng trong [`docs/superpowers/plans/`](docs/superpowers/plans/) — follow từng bước. Nếu chưa có plan, đề xuất plan trước khi code.
4. Tuân thủ hai nguyên tắc cốt lõi trong `CLAUDE.md` ở mọi quyết định
