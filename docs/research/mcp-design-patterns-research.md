# MCP Design Patterns — 12-Server Research Synthesis

> Saved 2026-05-18 as Phase 0 artifact for the M10.5 + M11 Tool UX + Architecture batch.
> Reference plan: `/home/tuan/.claude/plans/rippling-greeting-tulip.md`
> Source of truth for design decisions in Waves A-F. Every coding subagent MUST read this file before writing code.

## 1. Why this research

Users of odoo-semantic-mcp reported that today's 21 MCP tools return useful data but feel "incomplete and fragmented — hard to solve concrete Odoo development tasks". The tools work individually but don't chain into business workflows: drill-down requires guessing the next tool and copy-pasting composite keys; outputs are text-only so non-LLM consumers re-parse the tree; there is no centralized contract for how tools advertise their capabilities. Rather than guess at fixes, we surveyed 12 leading MCP servers from Anthropic, Microsoft, GitHub, Google, and Cloudflare — the cohort with the highest production traffic and the most diverse design space — to extract the eight cross-cutting patterns that consistently make MCP tools easier for AI clients (Claude Code, Cursor, ChatGPT, Gemini, VS Code) to chain into multi-step workflows. The output of that survey, distilled here, drives the six-wave optimization plan that this document accompanies.

## 2. The 12 servers (catalog)

### 2.1 Filesystem MCP — Anthropic
Source: `https://github.com/modelcontextprotocol/servers` (subdirectory `src/filesystem/`). The flagship reference server. Exposes 14 tools covering text/media reads, line-based edits with `dryRun` git-style diff preview, directory listing with size summaries, glob search, and an explicit `list_allowed_directories` capability advertisement. Every tool returns a dual-channel envelope: `content: [{type:"text", text}]` for LLM consumption and `structuredContent: {...}` carrying the same payload typed for programmatic consumers. Tools carry `annotations` (`readOnlyHint`, `idempotentHint`, `destructiveHint`) so clients can gate confirmation prompts. The MCP roots protocol is integrated so allowed directories can change at runtime via `roots/list_changed`.

### 2.2 Memory MCP — Anthropic
Source: `https://github.com/modelcontextprotocol/servers` (subdirectory `src/memory/`). A knowledge-graph store with 9 tools: `create_entities`, `create_relations`, `add_observations`, `delete_entities`, `delete_observations`, `delete_relations`, `read_graph`, `search_nodes`, `open_nodes`. Names are the only identifier — no synthetic UUIDs — and every mutation takes an array (no single-item endpoints) which trains the model to think in graph chunks. The canonical drill-down chain is `search_nodes → open_nodes`, with `open_nodes` filtering relations by an OR of endpoint names so the LLM discovers connections beyond its initial result set. Persistence is JSONL with one entity or relation per line, which is git-friendly and survives partial-write corruption. The relation convention is documented as "active voice" inside the tool description itself so the LLM keeps direction semantics consistent.

### 2.3 Sequential Thinking MCP — Anthropic
Source: `https://github.com/modelcontextprotocol/servers` (subdirectory `src/sequentialthinking/`). A single tool `sequentialthinking` whose entire value is the shape of its input schema: the LLM declares `thoughtNumber`, `totalThoughts`, `nextThoughtNeeded`, optional `isRevision`/`revisesThought`/`branchFromThought`/`branchId`/`needsMoreThoughts`. The server returns a receipt — `{thoughtNumber, totalThoughts, nextThoughtNeeded, branches[], thoughtHistoryLength}` — not the thought content itself, because the LLM already holds the content in its own context window. State lives server-side as an append-only history plus per-branch dicts, but only the meta-state is exposed. A `coercedBoolean` Zod preprocess accepts string `"false"` to defend against models that emit JSON-stringified booleans. The 50-line tool description is the actual product: it teaches the model when to revise, when to branch, when to extend.

### 2.4 Fetch MCP — Anthropic
Source: `https://github.com/modelcontextprotocol/servers` (subdirectory `src/fetch/`). Minimalist by design: one `fetch` tool and one `fetch` prompt. The schema is a four-field Pydantic model (`url`, `max_length=5000`, `start_index=0`, `raw=False`). HTML is converted to clean markdown via `readabilipy` + `markdownify`; non-HTML emits a warning prefix and serves raw bytes. The clever bit is pagination disclosure: when content is truncated, the response text appends `<error>Content truncated. Call the fetch tool with a start_index of {next_start} to get more content.</error>`. The cursor lives inline, not in metadata, so the LLM reads it and chains naturally. A distinct exhaustion sentinel `<error>No more content available.</error>` prevents looping past EOF. The user-agent splits manual versus autonomous fetches so robots.txt only blocks the autonomous path.

### 2.5 GitHub MCP — GitHub / Microsoft
Source: `https://github.com/github/github-mcp-server`. The largest production MCP server: ~105 tools across 20 toolsets covering repos, issues, PRs, code search, actions, security advisories, projects, and more. Three pagination flavors (page+per_page REST, opaque-cursor GraphQL, and a unified facade that converts internally). Tools follow a method-discriminator pattern (`issue_read(method=get|get_comments|get_sub_issues|get_labels)`) that subsumes four behaviours into one tool; the same trick is used for `pull_request_read`, `pull_request_review_write`, and `issue_write`. Outputs always wrap a `MarshalledTextResult` (JSON serialised as text), but the payloads themselves go through 37 `Minimal*` type adapters (e.g. `MinimalRepository` keeps 15 fields out of 100+) so the LLM never sees the verbose REST envelope. Toolset toggles (`--toolsets repos,issues`) and a dynamic-discovery toolset (`enable_toolset`, `list_available_toolsets`, `get_toolset_tools`) let clients page through capability at runtime. Per-toolset MCP `instructions` are appended to the server's preamble when a toolset enables, embedding workflow docs inside the schema. Schema snapshot tests under `pkg/github/__toolsnaps__/*.snap` (104 files) freeze each tool's contract.

### 2.6 Playwright MCP — Microsoft
Source: `https://github.com/microsoft/playwright` (under `packages/playwright-core/src/tools/backend/`). Famous for its accessibility-snapshot pattern: instead of screenshots, the server emits a YAML ARIA tree like `- button "Submit" [ref=e7] [cursor=pointer]`. Refs are short opaque IDs (`e1..eN`, with iframe prefix `f1e3`), minted by counter and stable across snapshots when backendNodeId persists. Every mutating tool (click, fill, navigate, press_key) auto-appends a fresh snapshot via `response.setIncludeSnapshot()`, so the LLM never asks for state separately. Targets accept either a ref or a Playwright selector — `targetLocators` regex-dispatches between them. Diff mode collapses unchanged subtrees to `- ref=eN [unchanged]`. The `fill_form` tool batches multiple ref-based mutations in one call with an explicit anti-chattiness nudge in its description. Stale refs surface as `Ref ${target} not found in the current page snapshot. Try capturing new snapshot.` — recovery named inline.

### 2.7 Azure MCP — Microsoft
Source: `https://github.com/Azure/azure-mcp`. Enterprise-scale design wrapping 35+ Azure services under `areas/<service>/src/AzureMcp.<Service>` with one `IAreaSetup` per area. Tool names flatten via `_` separator (`storage_account_list`, `cosmos_database_container_item_query`) creating an implicit namespace tree clients can list one subtree at a time. Every command derives from `SubscriptionCommand<T>` which injects `--subscription` with a three-tier fallback: CLI flag → `AZURE_SUBSCRIPTION_ID` env var → reject if both missing. Critically the sentinel strings `"subscription"` and `"default"` (which confused LLMs emit) are treated as empty so the env fallback fires. The meta-tool `azmcp_bestpractices get` returns prompt-grade `.txt` guidance (Azure codegen rules, security baseline) that the LLM is meant to read before writing code — a tool whose output is instructions, not data. Errors map `AuthenticationFailedException` to literal `"run az login"`, `RequestFailedException` to the upstream status, every error wrapped with an `aka.ms/azmcp/troubleshooting` link.

### 2.8 Chrome DevTools MCP — Google
Source: `https://github.com/ChromeDevTools/chrome-devtools-mcp`. About 46 tools across 10 categories (navigation, input, network, console, performance, memory, extensions, emulation, screenshot, snapshot). Page elements are referenced by `uid` strings minted from accessibility snapshots; pages get numeric `pageId`s; network requests get `reqid`; console messages get `msgid`. The canonical drill-down is `list_X` (compact) → `get_X` (detailed) using the same formatter class with a `fetchData` flag — `NetworkFormatter.toJSON()` versus `toJSONDetailed()` differ by 24 lines of extra fields. Cross-tool hints live in description strings as named tool references pulled from shared constants so links stay in sync after renames. The performance trace tool is a three-step pattern: `performance_start_trace` records, the stop call attaches a summary with `insightSetId`+`insightName` keys, then `performance_analyze_insight(insightSetId, insightName)` drills into a single insight with DevTools' own `PerformanceInsightFormatter`. Empty results return prose sentinels with next-action embedded: `"No recorded traces found. Record a performance trace so you have Insights to analyze."`

### 2.9 Google Drive MCP — Google / Anthropic
Source: `https://github.com/modelcontextprotocol/servers-archived` (subdirectory `src/gdrive/`). The headline architectural move is splitting content from discovery: the only tool is `search` (returns names + MIME types), while every file is exposed as an MCP Resource at the URI `gdrive:///<fileId>`. Hosts (Claude Desktop, web client) can pre-fetch resources, attach to context windows, or surface as `@`-mentions without tool calls. A MIME-driven export ladder centralises format conversion server-side: `application/vnd.google-apps.document` → `text/markdown`, spreadsheet → `text/csv`, presentation → `text/plain`, drawing → `image/png`. Text and JSON content go in the `text` field; binary content goes in `blob` as base64. Pagination uses MCP-native cursor mapping. The hosted Claude.ai variant extends this with explicit ID-returning tools (`search_files`, `read_file_content`, `get_file_metadata`, etc.) for clients that don't yet surface MCP Resources uniformly — the ID-content split is identical.

### 2.10 Postgres MCP — Anthropic + crystaldba
Sources: `https://github.com/modelcontextprotocol/servers-archived` (subdirectory `src/postgres/`, Anthropic reference) and `https://github.com/crystaldba/postgres-mcp` (community full-feature). The Anthropic reference is one `query` tool plus MCP Resources for schemas (`postgres://<host>/<table>/schema`). The crystaldba variant adds 8 tools enforcing a strict drill-down funnel: `list_schemas → list_objects(schema_name, object_type) → get_object_details(schema_name, object_name, object_type) → execute_sql`. `get_object_details` returns nested grouped JSON `{basic, columns, constraints, indexes}` so one round-trip after disambiguation gives the LLM everything to write a query. The `explain_query` tool produces a bespoke text tree (`→ Node (Cost: x..y) on table [Rows: n]`) instead of dumping raw JSON. Read-only safety is enforced by `pglast` AST whitelist of statement and function names, not regex; access-mode dynamic registration flips `destructiveHint`/`readOnlyHint` per mode. `analyze_db_health` puts a markdown bullet list of valid sub-checks inside its description so the LLM self-routes a single tool.

### 2.11 Slack MCP — Anthropic
Source: `https://github.com/modelcontextprotocol/servers-archived` (subdirectory `src/slack/`, archived 2025). Eight tools all prefixed `slack_` to avoid multi-MCP collisions. Identifiers are always raw IDs — `channel_id` (C-prefix), `user_id` (U-prefix), `thread_ts` (composite timestamp like `1234567890.123456`). Composite keys are passed as separate fields, never collapsed into a synthetic URI: `reply_to_thread(channel_id, thread_ts, text)`. Schema descriptions embed format-repair hints inline — for `thread_ts`: `"Timestamps without the period can be converted by adding the period such that 6 numbers come after it"` — so the LLM self-corrects malformed inputs before invoking. Outputs are pure JSON pass-through of Slack's Web API responses; the server does no reshape, mention resolution, or summarization. `SLACK_CHANNEL_IDS` env allow-list scopes the entire agent's universe at deploy time, fanning `list_channels` out to `conversations.info` per ID and synthesizing a Slack-shaped envelope so downstream code stays identical.

### 2.12 Cloudflare MCP — Cloudflare
Source: `https://github.com/cloudflare/mcp-server-cloudflare`. Ships 15 separate remote MCP servers, one per product surface, each reachable at `https://<surface>.mcp.cloudflare.com/mcp`. `workers-bindings` pragmatically aggregates 6 related surfaces (account, KV, Workers, R2, D1, Hyperdrive, docs) showing that split-by-surface is not dogmatic. The implicit-context pattern is the killer feature: `account_id` is stored on the agent Durable Object via `set_active_account`, then pulled by `agent.getActiveAccountId()` inside every tool — 80% of calls drop the param. Workers Observability uses TSV via `@fast-csv/format` for time-series tables — massively cheaper tokens than JSON. Error responses are centralised: every "no active account" call returns the same `MISSING_ACCOUNT_ID_RESPONSE` string defined once in `packages/mcp-common/src/constants.ts:3-10`. Dual auth (OAuth via `@cloudflare/workers-oauth-provider` plus raw API-token Bearer) lets users pick. Tool annotations are universal.

## 3. The 8 cross-cutting patterns

### Pattern 1 — Tool annotations advertise read-only-ness and destructiveness
**Adoption:** 8 of 12 servers (Filesystem, Memory, Sequential Thinking, GitHub, Playwright, Postgres crystaldba, Cloudflare, Chrome DevTools).

Every tool carries an `annotations` dict on its registration: `{readOnlyHint, idempotentHint, openWorldHint, destructiveHint}`. Clients use these to auto-approve safe tools (Cursor, VS Code skip confirmation UI for `readOnlyHint=true`), to render warning banners for destructive ones, and to feed policy engines. Postgres crystaldba flips its `execute_sql` annotations dynamically based on access mode — read-only mode registers it with `readOnlyHint=true`, full-access mode with `destructiveHint=true` — so the same tool advertises different safety to different deployments.

For LLM agents the value is twofold: first, fewer confirmation interruptions when chaining many cheap reads; second, an explicit signal that errors are reasoning errors, not state corruption, so the model can retry confidently.

**Adopted in Wave A WI-A1** — `READONLY_TOOL_KWARGS` constant applied via `@mcp.tool(**READONLY_TOOL_KWARGS)` to all 21 odoo-semantic tools (all are read-only against a static indexed graph). See plan §5 Wave A.

### Pattern 2 — Dual-channel `content` + `structuredContent` response envelope
**Adoption:** 7 of 12 (Filesystem, Memory, Sequential Thinking, Chrome DevTools, Playwright, Azure typed, GitHub via MarshalledTextResult).

Every tool returns both a human-readable text block (markdown tree, prose, or stringified JSON) and a typed `structuredContent` field carrying the same payload as a JSON object that matches the tool's declared `outputSchema`. The text channel feeds the LLM and the user; the structured channel feeds non-LLM consumers (admin UIs, eval harnesses, CI scripts, hosted IDE features) without forcing them to regex-parse the tree.

Filesystem's `outputSchema` is usually `{content: z.string()}` with payloads JSON-stringified inside; Chrome DevTools and Playwright build the structured side declaratively via `response.setIncludeNetworkRequests(true, {...})` and serialise lazily. The pattern is opt-in per tool — `structuredContent` is omitted when the payload truly has no structure (raw text dumps).

**Adopted in Wave B WI-B3** — odoo-semantic adds `output_schema=` to 7 priority tools (resolve_model/field/method/view + list_fields + list_methods + describe_module) and returns `ToolResult(content=[TextContent(...)], structured_content=...)`. The text channel stays byte-identical to current output (snapshot tests gate). See plan §5 Wave B.

### Pattern 3 — Next-step hints embedded in tool output, sourced from a single registry
**Adoption:** 9 of 12 (Fetch `<error>` tag, Cloudflare `MISSING_ACCOUNT_ID_RESPONSE`, Chrome DevTools prose sentinels, GitHub per-toolset instructions, Playwright stale-ref names recovery, Azure aka.ms/azmcp/troubleshooting, Postgres analyze_db_health description menu, Sequential Thinking processThought, Slack).

Successful MCP servers put the next-action instruction in the response text the LLM reads, not in metadata or out-of-band docs. Fetch literally writes `Call the fetch tool with a start_index of {next_start} to get more content` as a copy-pasteable literal. Cloudflare centralises the canonical "no active account" guidance into one constant `MISSING_ACCOUNT_ID_RESPONSE` shared across surfaces so the string never drifts. Chrome DevTools' empty-trace error names the recovery: `"Record a performance trace so you have Insights to analyze."`

odoo-semantic already has next-step footers (per ADR-0023 §4 — 18 drill-down tools emit `└─ Next: tool_a(...) | tool_b(...)`) but the strings are inline f-strings scattered across 25+ callsites in `src/mcp/server.py`, so updating a hint means hunting and replacing.

**Adopted in Wave A WI-A2** — extract scattered callsites into `src/mcp/hints.py` with `NEXT_STEP_HINTS: dict[str, list[str]]` and a `hints_for(tool, **ctx)` renderer. Text output stays byte-identical (snapshot tests gate). See plan §5 Wave A.

### Pattern 4 — `list_X` (compact) + `get_X` (detailed) share one formatter via a `fetchData` flag
**Adoption:** 8 of 12 (GitHub MinimalRepository/MinimalIssue, Chrome DevTools NetworkFormatter+ConsoleFormatter, Postgres crystaldba list_objects+get_object_details, Cloudflare list_namespaces+get_namespace, GDrive, Slack channels/threads, Memory open_nodes, Filesystem read_*).

The same formatter class produces both the compact list row (`toJSON()`, drops nested envelopes and bulk fields) and the detailed entity (`toJSONDetailed()`, includes headers, bodies, comments, redirect chains). The list-tool path passes `fetchData: false`; the detail-tool path passes `fetchData: true`. Same code, different verbosity — eliminates the drift that happens when `list_users` and `get_user` are written independently and slowly disagree on field names.

The pattern relies on two pillars: a shared DTO (Pydantic, Zod, or hand-rolled type) and a tree-rendering helper that knows how to format both shapes. Today odoo-semantic builds its tree text manually in each tool with hand-coded `├─`/`└─`/`│   ` indents — no shared TreeBuilder class.

**Adopted in Wave B WI-B1** — introduce `src/mcp/tree_builder.py` with a `TreeNode` class that auto-handles indent rules per ADR-0023 §1. Two PoC migrations (`_resolve_model`, `_list_fields`) prove the API. The remaining 5 of 7 priority tools migrate as part of WI-B3 dual-channel rollout. See plan §5 Wave B.

### Pattern 5 — Opaque ref IDs minted in list outputs for frictionless drill-down
**Adoption:** 4 of 12 (Playwright `e7/f1e3`, Chrome DevTools `uid/reqid/msgid/insightSetId`, Postgres crystaldba object_name as stable handle, Cloudflare resource IDs).

Playwright is the exemplar: `take_snapshot` returns a YAML tree where every interactable element gets a short `[ref=e7]` annotation, generated by a per-snapshot counter. `click({element: "Submit", target: "e7"})` looks the ref up via `page.locator('aria-ref=e7')`. The `target` parameter is dual-mode — regex `^(f\d+)?e\d+$` dispatches to ref lookup, anything else falls through to a Playwright selector. Refs are cheap (1-3 chars), distinguishable from selectors, and persist across snapshots when the underlying backendNodeId is stable. Chrome DevTools' `list_network_requests` and `list_console_messages` mint similar IDs (`reqid`, `msgid`) and pair them with `get_network_request(reqid)`/`get_console_message(msgid)`.

For odoo-semantic the value is huge: today `resolve_field` requires the LLM to repeat the composite key `(model='sale.order', field='amount_total')`. After refs, `list_fields` would emit `├─ field "amount_total" : Monetary [computed] [ref=f12]` and `resolve_field(target='f12')` would resolve via a 5-minute in-memory map scoped by api_key_id.

**Adopted in Wave C WI-C1+WI-C2+WI-C3** — create `src/mcp/refs.py` per-call minter; 6 list_* tools emit refs + accept `start_index` pagination; 4 resolve_* tools accept `target` as ref or canonical name (legacy `model_name=`/`field_name=` kwargs preserved with DeprecationWarning). See plan §5 Wave C.

### Pattern 6 — Implicit context via `set_active_*` tools and env-var fallback
**Adoption:** 3 of 12 (Cloudflare `set_active_account`, Azure `--subscription` with env-var fallback + sentinel defense, Sequential Thinking server-side history).

Cloudflare doesn't require `account_id` on every tool call. It's stored on the agent Durable Object via `set_active_account` and pulled by `getActiveAccountId()` inside each handler. Account-scoped tokens auto-set it from the API key; user tokens persist via a `UserDetails` Durable Object. Azure's `SubscriptionCommand<T>` resolves in three tiers: CLI flag → `AZURE_SUBSCRIPTION_ID` env var → reject. Crucially, sentinel strings `"subscription"`/`"default"` (LLM hallucinations of placeholder values) are treated as empty so the env fallback fires.

Today odoo-semantic requires `odoo_version` on every tool call (with `"auto"` falling back to `_latest_version()`). For a session that's working in one version, 80% of calls carry the param redundantly.

**Adopted in Wave E (entire wave, WI-E1 through WI-E5)** — new `api_key_session` Postgres table (PK = api_key_id, 24h sliding TTL); `src/mcp/session.py` read/write helpers with 60s in-memory cache; 4 new tools (`set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles`); sentinel defense rejecting `"auto"/"default"/"latest"/"version"/"current"`; resolution order explicit-arg → session → `_latest_version()`. See plan §5 Wave E.

### Pattern 7 — Method-discriminator consolidation collapses related tools
**Adoption:** 3 of 12 (GitHub `issue_read(method=get|get_comments|get_sub_issues|get_labels)` plus `issue_write`/`pull_request_read`/`pull_request_review_write`, Postgres `analyze_db_health(health_type=...)`, Cloudflare `worker_*` shape).

When a tool family shares signature and scope, consolidate into one tool with a discriminator parameter. GitHub's `issue_read` subsumes four behaviours (get the issue, get its comments, get sub-issues, get labels) into one registered tool; the JSON Schema enum on `method` self-documents the choices. Postgres' `analyze_db_health(health_type)` accepts `cache|connection|constraint|index|replication|sequence|vacuum|buffer` and puts the menu in the docstring as a markdown bullet list — the LLM self-routes a single tool. The shape is half the tool count without losing capability.

odoo-semantic has 10 tools that share `(model, module, odoo_version, profile_name)` signature: `resolve_model/field/method/view` and `list_fields/methods/views/owl_components/qweb_templates/js_patches`. They divide naturally along two axes: model-scoped versus module-scoped, and single-entity versus enumeration.

**Adopted in Wave D (entire wave, WI-D1 through WI-D6)** — three new superset tools: `model_inspect(model, method=fields|methods|views|owl, ...)`, `module_inspect(module, method=qweb|patches|views|owl, ...)`, `entity_lookup(kind=model|field|method|view, ...)` with typed args (not dotted-name parsing — avoids `sale.order.line.amount` ambiguity). Old 10 tools become deprecation shims with `DEPRECATED:` banner; removal scheduled v0.6. ADR-0028 documents rationale + timeline. See plan §5 Wave D.

### Pattern 8 — MCP Resources primitive with stable URIs for content fetching
**Adoption:** 3 of 12 (Google Drive `gdrive:///fileId`, Postgres Anthropic-reference schemas at `postgres://host/table/schema`, Filesystem via `roots/list_changed`).

The MCP Resources primitive separates content from discovery: tools surface IDs and search; resources surface bytes. Google Drive exposes every file as `gdrive:///fileId` so hosts can pre-fetch into context windows, attach to `@`-mentions, or surface in resource browsers without tool round-trips. Format conversion is centralised — Docs → markdown, Sheets → CSV, Drawings → PNG — so every AI client gets uniform content. Postgres lists every table schema as a resource so SQL-writing LLMs get column types out-of-band without a separate `describe_table` tool call.

odoo-semantic has stable composite keys for Module, Model, Field, Method, View, PatternExample, Stylesheet — perfect candidates for URI-addressable resources.

**Adopted in Wave F (entire wave, WI-F1 through WI-F5)** — URI scheme `odoo://{version}/model/{name}`, `odoo://{version}/model/{name}/field/{field}`, `odoo://{version}/module/{name}/manifest`, `odoo://{version}/view/{xmlid}`, etc. MIME-style content negotiation: Model → markdown, View → XML, Method → Python source slice, Field → JSON, Pattern → markdown with fenced code, Stylesheet → CSS. In-memory LRU cache (1000 entries, 300s TTL) for v1; Postgres-backed cache with head_sha invalidation deferred to M11 follow-up. `list_resources` returns top-100 most-depended-on models per indexed version (discovery funnel, not full enumeration). See plan §5 Wave F.

## 4. Anti-patterns to avoid

- **Raw API JSON pass-through** — Slack gets away with this only because Slack's Web API is already shaped for clients. Neo4j and pgvector rows are not. Dumping `{nodes: [...], relationships: [...]}` directly costs tokens and burdens the LLM with parsing. odoo-semantic should keep its tree-grammar renderer (`_render_capped`, ADR-0023) and add `structuredContent` alongside, not replace.

- **Search returns names without IDs** — the Google Drive Anthropic reference's `search` tool returns names + MIME types in plain text, omitting the file ID (`index.ts:159`). The drill-down chain breaks: the LLM cannot then read the file without round-tripping through `ListResources`. The hosted Claude.ai variant fixed this by exposing `search_files` that returns IDs. odoo-semantic must always echo the composite key or ref ID in every output line, even in the human-friendly tree.

- **Unbounded response without truncation** — Memory and the Anthropic Postgres reference both serialise the entire result set with no cap. For odoo-semantic's 1000+ modules x 100+ fields per model, this would routinely overflow the LLM context. ADR-0023 already mandates `_render_capped` with explicit total disclosure; Wave C extends this with `start_index`/`max_length` cursor pagination per Fetch.

- **Synthetic opaque URI for composite key** — Tempting to mint `odoo://17.0/sale.order@sale@17.0` as a single string, but Slack and Memory both deliberately keep composite keys decomposed (Slack passes `channel_id` + `thread_ts` separately; Memory uses names directly). LLMs handle adjacent typed fields more reliably than they parse opaque strings, and round-trips stay debuggable. odoo-semantic should keep `(name, module, version)` as three params, not collapse them into a URI.

- **Mixing destructive and read-only tools without annotations** — when a server exposes both, clients can't differentiate; users either get confirmation prompts on every cheap read (annoying) or on no calls at all (dangerous). The `annotations` dict is cheap to add.

- **Hard-failing on missing optional state instead of returning a recovery hint** — Memory's `add_observations` throws on missing entities while everything else silently dedups, creating one inconsistent failure mode. Prefer Chrome DevTools' prose sentinels (`"No recorded traces found. Record a performance trace ..."`) — the LLM reads the recovery step and self-corrects.

## 5. Quick-win vs big-bet ROI matrix

| Pattern adoption | Effort | Impact | Plan WI |
|------------------|--------|--------|---------|
| Tool annotations on 21 tools | XS | M | Wave A WI-A1 |
| Centralize next-step hints in one module | S | M | Wave A WI-A2 |
| Missing grammar test from ADR-0023 | S | M | Wave A WI-A3 |
| Self-mythology docstrings | XS | S | Wave A WI-A4 |
| TreeBuilder shared module | M | L | Wave B WI-B1 |
| Pydantic DTOs `*Ref`/`*Output` | S | M | Wave B WI-B2 |
| Dual-channel envelope on 7 tools | M | L | Wave B WI-B3 |
| Dual-channel test coverage | S | M | Wave B WI-B4 |
| Per-call ref minter | S | M | Wave C WI-C1 |
| `list_*` emit refs + start_index pagination | L | L | Wave C WI-C2 |
| `resolve_*` dual-mode target | M | L | Wave C WI-C3 |
| Drill-down ref tests | S | M | Wave C WI-C4 |
| Three discriminator routers | M | L | Wave D WI-D1 |
| `_list_views_core` refactor | S | M | Wave D WI-D2 |
| Register 3 new `@mcp.tool` wrappers | XS | L | Wave D WI-D3 |
| 10 deprecation shims | S | M | Wave D WI-D4 |
| Router + shim tests | S | M | Wave D WI-D5 |
| ADR-0028 publish | XS | M | Wave D WI-D6 |
| Migration 0005 session-state table | XS | M | Wave E WI-E1 |
| `session.py` module + cache | M | L | Wave E WI-E2 |
| 4 new tools + 15 resolver inserts | M | L | Wave E WI-E3 |
| Session lifecycle tests | S | M | Wave E WI-E4 |
| ADR-0029 publish | XS | M | Wave E WI-E5 |
| 7 resource handlers + LRU cache | L | XL | Wave F WI-F1 |
| `resources_index.py` top-100 popular | S | M | Wave F WI-F2 |
| Wire resources + 7 docstring updates | XS | M | Wave F WI-F3 |
| 3 resource test files | M | M | Wave F WI-F4 |
| ADR-0030 publish | XS | M | Wave F WI-F5 |

Reading the matrix: Wave A and Wave B are dominated by quick or modest wins with medium-to-large impact; Wave C and Wave D shift to larger effort with consistently large impact; Wave E and Wave F are the big bets (highest impact, highest effort) and ship in the second milestone (M11) once the foundation has baked.

## 6. Adoption roadmap: Wave A → F

### Wave A — Quick Wins (M10.5, 4 days)
Lands non-breaking foundations so every subsequent wave has a consistent baseline. Tool annotations (Pattern 1) advertise read-only-ness to all clients. The hints SSOT (Pattern 3 infrastructure) extracts 25+ scattered inline f-strings into `src/mcp/hints.py` so future hint changes are a single-file edit. The grammar consistency test fills the gap referenced but missing per ADR-0023 §4 — it gates future drift. Self-mythology docstrings on `lookup_core_api` and `find_deprecated_usage` borrow Fetch's pattern of overriding the LLM's RLHF prior to recall-from-training. Wave A is intentionally low risk: every WI is additive or replaces equal-length code, and the snapshot test suite gates that text output stays byte-identical.

### Wave B — Output Envelope (M10.5, 5 days)
Adopts Patterns 2 and 4: dual-channel response envelope and shared `TreeBuilder`. The TreeBuilder migration is gated by two PoC tools (`resolve_model`, `list_fields`) that must produce byte-identical output before the API is allowed to spread. Pydantic DTOs (`ModelRef`, `FieldRef`, `MethodRef`, `ViewRef`, `ModuleRef`, `PatternRef`, `CoreSymbolRef` plus 7 `*Output` wrappers) provide the typed schemas; FastMCP 2.14.7's `output_schema=` parameter plus `ToolResult(content=..., structured_content=...)` returns wires them up. Scope is intentionally 7 of 21 tools — the most-used drill-down family — to validate the pattern before broad rollout in Wave D consolidation.

### Wave C — Drill-down Cohesion (M10.5, 7 days)
Adopts Pattern 5 (opaque ref IDs) plus pagination cursors (Fetch-style `start_index`+continuation hint). `src/mcp/refs.py` mints per-call refs scoped by api_key_id with a 5-minute TTL in-memory map; stale-ref errors name the recovery path. The 6 `list_*` tools emit `[ref=fN]` per row and accept `start_index`; the 4 `resolve_*` tools accept dual-mode `target` (ref or canonical). Legacy kwargs (`model_name=`, `field_name=`) are preserved with DeprecationWarning indefinitely because AI client configs are hard to update at the user end. The continuation hint uses plain text (not Fetch's `<error>` tag) — pagination is routine, not failure. M10.5 batch (Wave A + B + C, 16 WIs, ~16 engineer-days) closes here.

### Wave D — Discriminator Consolidation (M11, 5 days)
Adopts Pattern 7: three new superset tools (`model_inspect`, `module_inspect`, `entity_lookup`) subsume 10 existing tools via method/kind discriminators. The split between `model_inspect` and `module_inspect` (rather than one mega `inventory` tool) avoids nullable-required-param confusion when scope axes differ. `entity_lookup` takes typed args (`kind`, `model`, `field`, `method_name`, `xmlid`) rather than parsing dotted names — `sale.order.line.amount` is ambiguous between "field on sale.order" and "field on sale.order.line", and the disambiguation UX is poor. Old 10 tools become shims with `DEPRECATED:` banner in output + docstring SKIP clause; FastMCP 2.14.7 lacks a `_meta.deprecated` field so the in-output banner is the contract. Removal scheduled v0.6 (one major release later). ADR-0028 publishes the consolidation rationale and naming convention.

### Wave E — Implicit Context (M11, 4 days)
Adopts Pattern 6: per-API-key sticky session state for `odoo_version` and `profile_name`. New Postgres `api_key_session` table (PK on api_key_id, FK CASCADE on key deactivate, 24h sliding TTL via `updated_at`). `src/mcp/session.py` provides read/write helpers with a 60s in-memory cache for hot-path optimization; DB is the source of truth. Four new tools: `set_active_version`, `set_active_profile`, plus cold-start `list_available_versions` and `list_available_profiles`. Sentinel defense rejects LLM hallucinations like `"auto"`, `"default"`, `"latest"`, `"version"`, `"current"`. Resolution order across 15 resolver callsites: explicit arg → session active → `_latest_version()` fallback. Tenant isolation enforced via PK-on-api_key_id; session does not leak across keys. ADR-0029 documents the table, TTL, cache staleness window, and tenant guarantees.

### Wave F — MCP Resources (M11, 8 days)
Adopts Pattern 8: 7 resource handlers expose Neo4j entities at stable `odoo://` URIs. The URI grammar is `odoo://{version}/{kind}/{path}` with seven kinds (model, model/field, model/method, module, module/manifest, view, pattern, stylesheet). MIME-style content negotiation maps each kind to its natural format: Model markdown, View XML, Method Python slice, Field JSON, Pattern markdown with fenced code, Stylesheet raw CSS. In-memory LRU cache holds 1000 entries with 300s TTL — short enough that incremental reindexes propagate within five minutes, simple enough to ship without cross-process pub/sub. The discovery channel `list_resources()` returns top-100 most-depended-on models per indexed version (capped to avoid the 10k-entry UI blowup of full enumeration). Postgres-backed cache with `head_sha` invalidation is deferred to a follow-up because it requires cross-worker pub/sub that the M11 schedule cannot absorb. ADR-0030 publishes the URI scheme, MIME mapping, and cache policy.

## 7. References

### MCP server source repos
- Filesystem MCP — `https://github.com/modelcontextprotocol/servers` (`src/filesystem/`)
- Memory MCP — `https://github.com/modelcontextprotocol/servers` (`src/memory/`)
- Sequential Thinking MCP — `https://github.com/modelcontextprotocol/servers` (`src/sequentialthinking/`)
- Fetch MCP — `https://github.com/modelcontextprotocol/servers` (`src/fetch/`)
- GitHub MCP — `https://github.com/github/github-mcp-server`
- Playwright MCP — `https://github.com/microsoft/playwright` (`packages/playwright-core/src/tools/backend/`)
- Azure MCP — `https://github.com/Azure/azure-mcp`
- Chrome DevTools MCP — `https://github.com/ChromeDevTools/chrome-devtools-mcp`
- Google Drive MCP — `https://github.com/modelcontextprotocol/servers-archived` (`src/gdrive/`)
- Postgres MCP (Anthropic reference) — `https://github.com/modelcontextprotocol/servers-archived` (`src/postgres/`)
- Postgres MCP (community full-feature) — `https://github.com/crystaldba/postgres-mcp`
- Slack MCP — `https://github.com/modelcontextprotocol/servers-archived` (`src/slack/`)
- Cloudflare MCP — `https://github.com/cloudflare/mcp-server-cloudflare`

### odoo-semantic ADRs referenced by this synthesis
- ADR-0013 — Defined-in ranking heuristic (`docs/adr/0013-defined-in-ranking-heuristic.md`)
- ADR-0016 — Profile hierarchy (`docs/adr/0016-profile-hierarchy.md`)
- ADR-0023 — Tool output completeness (`docs/adr/0023-tool-output-completeness.md`)
- ADR-0026 — RBAC + key ownership (`docs/adr/0026-rbac-key-ownership.md`)

### Master plan
- `/home/tuan/.claude/plans/rippling-greeting-tulip.md` — six-wave roadmap with per-WI Intent/Outcome/Acceptance Criteria and subagent brief template.
