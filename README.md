# Odoo Semantic MCP (OSM)

AI coding assistants hallucinate Odoo field names, miss inheritance chains, and suggest deprecated APIs. OSM grounds your AI against a verified index of 12,000+ models and 184,000+ fields across Odoo v14-v19 (as of 2026-06). No running Odoo database required.

**12,000+ models** | **184,000+ fields** | **v14-v19 (actively maintained)** | **31 MCP tools** | **9 resources** | **95% vs 43% accuracy on real tasks** | **Free: 1,000 tool calls/month**

---

## The Problem

When AI coding tools (Claude Code, Codex, Gemini) work with Odoo, they routinely:

- Hallucinate field and method names that do not exist in the targeted version
- Miss all module extensions on `sale.order` and treat a 15-module inheritance chain as a simple model
- Cannot trace the XPath override chain of a view across multiple modules
- Change a field like `amount_total` with no knowledge of what breaks downstream

OSM fixes this by indexing the full Odoo codebase (cross-repo, cross-version) into a graph database and vector store, then exposing 31 query tools over MCP so any AI client can ground its answers against real source truth.

---

## Quick Start

**Option 1 - Try instantly (no account required):**

```bash
claude mcp add --transport http https://odoo-semantic.viindoo.com/mcp/demo
```

The demo endpoint gives you 20 read-only queries with a shared public key. No signup needed.

**Option 2 - Full access (free tier: 1,000 tool calls/month):**

1. [Sign up for a free API key](https://odoo-semantic.viindoo.com/signup/) - 30 seconds, no credit card
2. Visit **https://odoo-semantic.viindoo.com/install/**, paste your API key, copy the snippet for your AI tool

### Claude Code (plugin path)

Two free plugins (MIT): `odoo-semantic-mcp` (MCP config) and `odoo-ai-agents` (42 skills, 8 agents, 10 commands). The skills plugin pulls in the MCP plugin automatically:

```bash
claude plugin marketplace add Viindoo/claude-plugins --scope user
claude plugin install odoo-ai-agents@viindoo-plugins --scope user
```

Then inside Claude Code: `/odoo-semantic-mcp:connect` to enter your URL and API key.

> MCP-only (no persona skills)? Install just `odoo-semantic-mcp@viindoo-plugins` instead.
> Manual MCP setup or self-hosted? See [manual MCP setup](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/setup.md#manual-mcp-setup-advanced--self-hosted).

> **Migrating from `odoo-semantic-skills`?** The plugin has been renamed to `odoo-ai-agents`. Uninstall the old plugin (`claude plugin uninstall odoo-semantic-skills@viindoo-plugins`) and install `odoo-ai-agents@viindoo-plugins` using the command above.

### Verify after install

After connecting, verify the MCP server loaded correctly:

```
"Using odoo-semantic, list the inheritance chain of sale.order on Odoo 17.0."
```

The agent should call `model_inspect` or `entity_lookup` and return a real chain. If it answers from memory without calling an MCP tool, the connection did not register - restart Claude Code after running `/odoo-semantic-mcp:connect`.

For other AI tools (Cursor, Codex CLI, Gemini CLI, VS Code, Windsurf, JetBrains AI Assistant, Continue.dev): see the [client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/setup.md).

---

## Pricing

Free tier: 1,000 tool calls/month. Paid plans from $19/seat/month.

| Plan | Tool calls/month | Rate limit | Price |
|------|-----------------|------------|-------|
| Free | 1,000 | 30 calls/min | $0 |
| Pro | 10,000 | 120 calls/min | $19/seat/mo (up to 5 seats) |
| Team | 100,000 | 300 calls/min | $39/seat/mo (min 3 seats - from $117/mo) |

[Get your free API key](https://odoo-semantic.viindoo.com/signup/) - no credit card required.

---

## Accuracy Benchmark

Measured on 40 real-world Odoo coding tasks: field name lookup, inheritance chain traversal, view override detection, ORM query construction.

| Condition | Correct answers | Typical errors |
|-----------|----------------|----------------|
| AI without OSM | 43% | Hallucinated fields, wrong versions, missed module extensions |
| AI with OSM | 95% | - |

**Methodology:** Tested with Claude claude-sonnet-4-5 on 40 real-world Odoo coding tasks across field lookups, inheritance chain traversal, view override detection, and ORM construction. Measurement date: 2026-05. A task is counted failed if the AI produces a field name, model name, or ORM call that does not exist in the target Odoo version. 2 tasks failed: both involved private module extensions not present in the public index. Baseline (43%) uses the same model with no MCP context on the same task set.

The gap comes from OSM replacing model inference with graph lookup. Every field, method, and view XML ID is resolved against the indexed source, not predicted from training data.

---

## Upgrade Risk Scanner

Running Odoo v14? Support ends October 31, 2026. OSM's upgrade risk scanner compares your installed modules against breaking changes between versions and flags fields, methods, and ORM patterns that will fail after upgrade.

What it catches:

- Fields removed or renamed between versions (e.g., `sale.order.picking_policy` moved in v16)
- Methods deprecated in the target version
- Domain syntax that was silently accepted in v14 but raises an error in v16+
- Compute method signatures that changed

Example:

```
"Using odoo-semantic, scan my custom module sale_custom for v14 deprecations that break in v17."
```

Sample output:

```
3 breaking changes found in sale_custom:
1. Field `sale.order.invoice_ids` - compute method signature changed in v15. Update _compute_invoice_ids to remove deprecated argument.
2. Method `account.move._get_reconciled_info_JSON_values` removed in v15. No direct replacement - see OpenUpgrade migration guide.
3. ORM call uses deprecated `count` kwarg in v16. Replace with len(self.env['sale.order'].search(...)).
```

Odoo v14 EOL: October 31, 2026. Start your upgrade assessment with the `find_deprecated_usage` tool.

---

## How It Works

```
Odoo repos (~/git/*_17.0/)
        |
        v  indexed once on the server
+----------------------------------------------+
|  Indexer Pipeline                            |
|  Neo4j + pgvector                            |
|                                              |
|  FastAPI JSON API  (port 8003)               |
|  Astro SSR + React islands  (port 4321)      |
|  MCP Server  (port 8002)                     |
+---------------------+------------------------+
                      | nginx routes:
                      |  /api/waitlist     -> 8003
                      |  /api/*            -> 8003
                      |  /mcp              -> 8002
                      |  /install/         -> 8002
                      |  /health           -> 8002 (liveness - no DB I/O)
                      |  /ready            -> 8002 (readiness, cached 60s)
                      |  /metrics          -> 8002 (Prometheus, IP-restricted)
                      |  /                 -> 4321 (Astro SSR, catch-all)
                      v
  Claude Code / VS Code / Codex / Gemini
  (add URL to config -- nothing to install)
```

When your AI tool calls an MCP tool like `model_inspect`, OSM queries the indexed graph directly - not a language model's training memory. This is why the accuracy gap is 52 points: graph lookup does not hallucinate. Either the field exists in the index at that version, or it does not.

**Example - resolving an ORM chain:**

```json
{
  "tool": "resolve_orm_chain",
  "arguments": {
    "model": "sale.order",
    "chain": "order_line.product_id.categ_id.complete_name",
    "version": "17"
  }
}
```

Response (truncated):

```json
{
  "resolved": true,
  "steps": [
    {"field": "order_line", "model": "sale.order", "type": "One2many", "comodel": "sale.order.line"},
    {"field": "product_id", "model": "sale.order.line", "type": "Many2one", "comodel": "product.product"},
    {"field": "categ_id", "model": "product.product", "via": "product.template", "type": "Many2one", "comodel": "product.category"},
    {"field": "complete_name", "model": "product.category", "type": "Char"}
  ]
}
```

---

## How OSM Fits With Your Existing Tools

OSM is not a replacement for IDE language servers or local code analysis tools. It is a semantic knowledge layer that your AI assistant queries at code-generation time. The tools below serve different purposes and can be used alongside OSM:

| Tool | Category | What it does | Relationship to OSM |
|------|----------|-------------|---------------------|
| OSM (this server) | Hosted semantic graph | Answers AI agent queries against an indexed cross-version codebase | - |
| odoo-ls | Language server (IDE) | Syntax checking, autocompletion, go-to-definition in your editor for a single local checkout | Complementary - odoo-ls checks syntax in your IDE; OSM answers semantic questions across all versions |
| Akaidoo | Local context tool | Reads your local Odoo source and loads it into the AI context window | Complementary - works on your local checkout; OSM covers all 12 versions without local files |
| Database bridge MCP servers | Live data tools | Read and write live Odoo business records from a running instance | Different use case - they retrieve records; OSM understands source structure |

---

## MCP Tools (31)

OSM exposes 31 tools grouped by function. All tools are read-only against the knowledge index. Full routing matrix with trigger conditions and persona mapping: [mcp-tool-routing.md](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/reference/mcp-tool-routing.md).

### Core tools (10)

| Tool | What it answers |
|------|----------------|
| `find_examples` | Where is this pattern used in the codebase? |
| `impact_analysis` | What breaks if I change this field or method? |
| `lookup_core_api` | What does this Odoo method do, what are its arguments? |
| `api_version_diff` | What changed in this API between two versions? |
| `find_deprecated_usage` | Which custom modules use deprecated APIs? |
| `lint_check` | Does this code violate Odoo conventions? |
| `cli_help` | What does this Odoo CLI command do? |
| `suggest_pattern` | What is the standard pattern for this Odoo use case? |
| `check_module_exists` | Is this module in CE, EE, or a Viindoo addon? Which versions? |
| `find_override_point` | Where and how should I override this method? |

### Entity and module overview (4)

| Tool | What it answers |
|------|----------------|
| `model_inspect` | Full field list, methods, inheritance chain, views of a model |
| `module_inspect` | Module manifest, dependencies, models, views, assets |
| `entity_lookup` | List all fields / methods / views matching a pattern |
| `describe_module` | Human-readable module overview for documentation or pre-sales |

### Session context (4)

| Tool | What it does |
|------|-------------|
| `set_active_version` | Pin this API key to an Odoo version (24h TTL) - eliminates the `odoo_version` parameter on every call |
| `set_active_profile` | Switch to a named indexing profile |
| `list_available_versions` | List versions available to this API key |
| `list_available_profiles` | List profiles available to this API key |

### Stylesheet tools (2)

| Tool | What it answers |
|------|----------------|
| `resolve_stylesheet` | Full SCSS import chain for a stylesheet file |
| `find_style_override` | Which modules override a specific CSS variable or selector? |

### ORM validation tools (4)

Static analysis - no running Odoo needed.

| Tool | What it validates |
|------|------------------|
| `resolve_orm_chain` | Is this dotted-path expression valid on this model? |
| `validate_domain` | Are all fields and operators in this domain valid for this model and version? |
| `validate_depends` | Are all paths in this `@api.depends()` expression valid? |
| `validate_relation` | Does this field point to a real comodel? |

**Example - what `validate_domain` catches:**

Bad domain (AI-generated, will fail at runtime):
```python
[('total_amount', '>', 100), ('partner_id.country', '=', 'VN')]
```

`validate_domain` output:
- Error: field `total_amount` not found on `sale.order` (the field is `amount_total`).
- Error: field `country` not found on `res.partner` — did you mean `country_id`?

Fixed domain:
```python
[('amount_total', '>', 100), ('partner_id.country_id.code', '=', 'VN')]
```

### Profile introspection (1)

| Tool | What it answers |
|------|----------------|
| `profile_inspect` | Which repos and modules make up this indexing profile? |

### Test surface tools (6)

| Tool | What it answers |
|------|----------------|
| `find_test_examples` | Where is this test pattern used in the codebase? |
| `tests_covering` | Which tests cover a model, field, or method? |
| `test_class_inspect` | Full inheritance chain, setUp methods, and subclasses of a test class? |
| `test_base_classes` | Framework base-class menu and cursor/transaction contract? |
| `test_coverage_audit` | Which fields/methods have zero test coverage in a module? |
| `js_test_inspect` | Frontend JS test suites (Hoot/QUnit/tour) in a module? |

---

## MCP Resources (9)

Resources are URI templates for bookmark-stable entity reads via the `odoo://` URI scheme. The `{version}` segment accepts `auto`, `default`, or `latest` to resolve against the API key's active version. Resource bodies are cached (LRU 1000 entries, 300s TTL, per resolved version).

| URI template | Content | MIME |
|---|---|---|
| `odoo://{version}/model/{name}` | Full model tree (fields, methods, inheritance, views) | text/markdown |
| `odoo://{version}/field/{model}/{field}` | Field definition and related metadata | text/markdown |
| `odoo://{version}/method/{model}/{method}` | Method source and docstring | text/markdown |
| `odoo://{version}/view/{xmlid}` | View XML and override chain | text/markdown |
| `odoo://{version}/module/{name}` | Module overview (same as `describe_module`) | text/markdown |
| `odoo://{version}/pattern/{pattern_id}` | Curated pattern snippet with gotchas | text/markdown |
| `odoo://{version}/stylesheet/{module}/{file_path*}` | Raw CSS/SCSS source | text/css / text/x-scss |
| `odoo://{version}/test/{module}/{class_name}` | Test class definition, inheritance, and methods | text/markdown |
| `odoo://{version}/testcoverage/{model}` | Test coverage audit for a model | text/markdown |

---

## Test Surface Indexing

OSM now indexes the entire automated-test surface across Odoo v8-v19: test classes, test methods, test helpers, and frontend JS test suites (Hoot, QUnit, web tours). This includes class inheritance chains, static field/method coverage, and framework base-class semantics. Agents can now ground test decisions against real test patterns, discover what tests exist, and understand cursor/transaction contracts — eliminating the need to reinvent test approaches or misuse `cr.commit()` in an isolation context. Test metadata is indexed as Neo4j nodes (TestClass/TestMethod/TestHelper/JsTestSuite) with coverage edges (COVERS_MODEL/COVERS_FIELD/COVERS_METHOD) linked to the production model graph. All 6 test tools + 2 test resources are zero-migration additions.

---

## Persona Guides

Choose the guide that matches your role to see which OSM tools are most relevant to your workflow.

| Persona | Primary tools | Guide |
|---------|--------------|-------|
| CEO / Manager | `impact_analysis`, `check_module_exists`, `find_deprecated_usage` | [CEO Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/personas/ceo.md) |
| Developer | `model_inspect`, `find_override_point`, `suggest_pattern`, `lint_check` | [Dev Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/personas/dev.md) |
| Consultant | `check_module_exists`, `find_examples`, `lookup_core_api` | [Consultant Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/personas/consultant.md) |
| Marketer | `api_version_diff`, `find_examples` | [Marketer Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/personas/marketer.md) |
| Sales | `check_module_exists`, `find_examples`, `model_inspect` | [Sales Guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/personas/sales.md) |

---

## Documentation

The following reference documents cover installation, tool parameters, and advanced configuration.

| File | Content |
|------|---------|
| [Client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/setup.md) | End-user client setup: Claude Code, Codex, Gemini, VS Code, and more |
| [`docs/deploy.md`](docs/deploy.md) | Admin deploy guide: DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
| [`docs/deploy/pre-launch-checklist.md`](docs/deploy/pre-launch-checklist.md) | Pre-launch signoff: 10-item verify + MCP tool sign-off table + resource sign-off |
| [`docs/deploy/go-live-checklist.md`](docs/deploy/go-live-checklist.md) | Go-live ops checklist: ordered operator actions (RLS cutover, backups, SMTP, rate-limit, signup) |
| [`docs/deploy/disaster-recovery.md`](docs/deploy/disaster-recovery.md) | DR runbook: backup frequency, restore order, step-by-step commands, RTO estimate |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Developer setup, running tests, Local E2E, workflow |
| [`CHANGELOG.md`](CHANGELOG.md) | Full release notes |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records |
| [MCP tool routing matrix](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/reference/mcp-tool-routing.md) | Full routing matrix: 31 tools, trigger conditions, persona mapping |

---

## Frequently Asked Questions

**What is the difference between OSM and an Odoo language server like odoo-ls?**

An Odoo language server is built for IDE features: hover, go-to-definition, autocomplete. It works on a single local checkout. OSM is built for AI query tools: it indexes 12 Odoo versions in one graph, exposes 31 query tools over MCP, and answers questions like "what breaks if I change this field" across all module extensions in the graph. Both can be active simultaneously - odoo-ls checks syntax in your IDE while OSM answers semantic questions that require the full cross-version index.

**Does OSM require a running Odoo instance?**

No. OSM indexes source code only. You do not need an Odoo database, an Odoo server process, or admin credentials to any Odoo environment. This is the key difference from Odoo database bridge MCP servers, which read live business records from a running instance.

**Which AI tools does OSM work with?**

Any tool that supports MCP natively: Claude Code, Cursor, Codex CLI, Gemini CLI, VS Code (v1.99+), Zed, Windsurf, JetBrains AI Assistant, and Continue.dev. ChatGPT requires a separate MCP-compatible bridge layer and does not connect directly. Configuration snippets for each tool are in `odoo-mcp-client/plugins/odoo-ai-agents/snippets/`.

**How accurate is OSM compared to ungrounded AI?**

On 40 real-world Odoo coding tasks tested with Claude claude-sonnet-4-5, AI grounded with OSM answered correctly 95% of the time. Without OSM, the same model answered correctly 43% of the time. The gap is largest on inheritance chain traversal and field version questions, where AI training data is sparse and hallucination rates are highest. See the Accuracy Benchmark section above for methodology.

**Which Odoo versions does OSM support?**

v14-v19 are actively maintained. v8-v13 are available but receive no active index updates. All 12 versions (v8-v19) are indexed and queryable in a single API session. Use `set_active_version` to pin your session to one version.

**Is OSM open source?**

The MCP client library (`odoo-mcp-client`, the package you install locally) is MIT-licensed. The indexer and server source code are AGPL-3.0. The hosted service at odoo-semantic.viindoo.com runs that server code and is available under a separate API-key access agreement. Self-hosting requires the server repository, available to registered users.

---

## Status

Latest release: **v0.18.0**. Tool count: **31** (test surface index adds 6 tools + 2 resources). See [CHANGELOG.md](CHANGELOG.md) for full release notes.

---

## Deploy Server (Admin)

> This is a private Viindoo repository. Cloning requires org membership or a granted deploy key.

```bash
git clone https://github.com/Viindoo/odoo-semantic-server && cd odoo-semantic-server
make install && docker compose up -d
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate

# Build Astro frontend (requires Node.js 22+, pnpm 10+):
cd site && pnpm install --frozen-lockfile && pnpm build && cd ..
```

After that: register a profile, index repos, generate `FERNET_KEY` and an API key, start the three systemd services (MCP :8002, FastAPI :8003, Astro :4321).

- [`docs/deploy.md`](docs/deploy.md) for full production setup: all-in-one vs split-tier, systemd, nginx, TLS, backup.
- System requirements: minimum 2 vCPU / 8 GB RAM, recommended 4 vCPU / 16 GB for full tool set. Node.js 22+ and pnpm 10+ required for the Astro frontend.
