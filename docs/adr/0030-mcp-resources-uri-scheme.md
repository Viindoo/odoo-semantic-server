# ADR-0030 — MCP Resources URI Scheme: `odoo://` Protocol, MIME Mapping, and LRU Cache Policy

**Date:** 2026-05-19
**Milestone:** M11 Wave F

---

## Status

Accepted

---

## Context

The MCP protocol defines two distinct primitives for surfacing server-side data to AI clients: **tools** (callable functions that accept arguments and return computed results) and **resources** (stable, addressable content that clients can subscribe to, pre-fetch into context windows, or surface as `@`-mentions without a tool round-trip). As of v0.5.x, odoo-semantic uses only the tools primitive. All 21 tools require the LLM to construct a tool-call request, wait for a round-trip, and parse the tree-text response even when the desired content — a model's field list, a view's XML source, a method's Python implementation — is stable between reindexes.

Research across 12 production MCP servers (internal design notes, Pattern 8) identified three servers that already treat content as resources rather than tool results:

- **Google Drive MCP** (`gdrive:///fileId`): every file is an MCP Resource. Hosts (Claude Desktop, Cursor) pre-fetch resources into context windows; the only tool is `search`, which returns IDs. Format conversion (Docs → markdown, Sheets → CSV, Drawings → PNG) is centralised server-side.
- **Postgres MCP (Anthropic reference)**: each table's schema is addressable at `postgres://<host>/<table>/schema`. SQL-writing LLMs get column types out-of-band without a `describe_table` tool call.
- **Filesystem MCP**: MCP roots (`roots/list_changed`) notify clients when allowed directories change, enabling pre-fetch invalidation without polling.

odoo-semantic's Neo4j graph already stores stable composite keys for every indexed entity:

| Entity type | Composite key |
|---|---|
| Model | `(name, module, odoo_version)` |
| Field | `(model_name, name, module, odoo_version)` |
| Method | `(model_name, name, module, odoo_version)` |
| View | `(xmlid, odoo_version)` |
| Module | `(name, odoo_version)` |
| PatternExample | `(name, odoo_version)` |
| Stylesheet | `(file_path, module, odoo_version)` |

These keys are stable within a single index run and only change when the indexer detects a code change in the corresponding repo (incremental indexer, ADR-0007). They are natural candidates for URI-addressable resources.

Two concrete problems motivate adding MCP Resources:

1. **Round-trip tax for static content.** A developer looking up `sale.order`'s field list, a view's XML, or a method's Python source makes separate tool calls and waits for each Neo4j round-trip. Once the index is current, this content does not change until the next reindex. A resource cache eliminates redundant round-trips within a 300-second window — matching the granularity of an interactive coding session.

2. **`resources/list` blowup risk.** A naive `list_resources` returning every indexed entity would produce tens of thousands of entries (10+ Odoo versions × 400+ modules × 100+ models × 50+ fields per model). MCP clients such as Claude Desktop render the resource list in a sidebar widget; a 100,000-entry list would freeze or crash the UI. The discovery channel must be capped at a manageable ceiling.

---

## Decision

### URI Scheme Grammar

The `odoo://` URI scheme follows this grammar:

```
odoo-uri    = "odoo://" version "/" kind "/" path
version     = odoo-version | "auto"
odoo-version = 1*DIGIT "." 1*DIGIT            ; e.g. "17.0", "16.0"
auto        = "auto"                           ; resolved via session.resolve_version_v2
kind        = "model" | "field" | "method" | "view"
            | "module" | "pattern" | "stylesheet"
path        = kind-specific-segment           ; see table below
```

The `"auto"` sentinel in the `version` segment is resolved via `session.resolve_version_v2` (the same three-tier resolution order defined in ADR-0029: explicit kwarg → session active → `_latest_version()` fallback). The sentinel is normalised server-side at resource-read time; the resolved version is logged alongside the cache lookup for observability.

**Kind-to-path mapping:**

| Kind | URI template | Example |
|---|---|---|
| `model` | `odoo://{version}/model/{name}` | `odoo://17.0/model/sale.order` |
| `field` | `odoo://{version}/field/{model}.{field}` | `odoo://17.0/field/sale.order.amount_total` |
| `method` | `odoo://{version}/method/{model}.{method}` | `odoo://17.0/method/sale.order._compute_amount` |
| `view` | `odoo://{version}/view/{xmlid}` | `odoo://17.0/view/sale.view_order_form` |
| `module` | `odoo://{version}/module/{name}` | `odoo://17.0/module/sale` |
| `pattern` | `odoo://{version}/pattern/{name}` | `odoo://17.0/pattern/computed-field-depends` |
| `stylesheet` | `odoo://{version}/stylesheet/{module}/{filename}` | `odoo://17.0/stylesheet/web/web.scss` |

URI path components follow the same normalisation rules as the existing composite keys in Neo4j: lowercase, dots preserved for dotted model names (`sale.order`), slashes used only as segment separators. A URI that does not match any indexed entity returns a 404-style resource error with a next-step hint naming the equivalent tool call (e.g., `"Entity not found — call resolve_model(model='sale.order', odoo_version='17.0') to check the indexed corpus."`).

### MIME-Type Content Negotiation

Each resource kind maps to a single canonical MIME type. The mapping is determined by the content's natural format for consumption — the format a developer would open in an editor to read or edit the content:

| Kind | MIME type | Content |
|---|---|---|
| `model` | `text/markdown` | Tree-text matching `resolve_model` body (field list, inheritance chain, method count) |
| `field` | `application/json` | JSON object: `{name, ttype, required, store, compute, related, string, module}` |
| `method` | `text/x-python` | Python source slice of the method body as indexed by the parser |
| `view` | `text/xml` | Raw XML of the view arch as indexed |
| `module` | `text/markdown` | Tree-text matching `describe_module` body (manifest summary, file counts, dependency graph) |
| `pattern` | `text/markdown` | Pattern description + fenced Python code block from `PatternExample` node |
| `stylesheet` | `text/css` | Raw CSS or SCSS source as indexed; MIME is `text/css` for both CSS and SCSS to remain RFC-compliant |

The rationale for `text/markdown` on model and module (rather than JSON) is that these resources are the primary input to LLM context windows. Markdown is already the format produced by `resolve_model` and `describe_module`, and LLMs consume prose-structured markdown more reliably than they parse raw JSON graph envelopes. JSON is reserved for fields where the content is inherently record-shaped and consumed programmatically (e.g., a field descriptor used to generate a form widget).

### Resource Handlers (`src/mcp/resources.py`)

Seven resource handlers are registered via `@mcp.resource("odoo://...")` decorators in `src/mcp/resources.py`. Each handler:

1. Parses the URI to extract `version`, `kind`, and `path` segments.
2. Resolves `version="auto"` via `session.resolve_version_v2` if present.
3. Checks the LRU cache (see Cache Policy below). On hit: returns cached bytes.
4. On miss: executes the corresponding Neo4j Cypher query (reusing the same query as the equivalent `_resolve_*` tool function to avoid drift).
5. Formats the result into the MIME type for the kind.
6. Stores the formatted bytes in the LRU cache with the current monotonic timestamp.
7. Returns the bytes plus the MIME type to the MCP framework.

Authentication is inherited from the MCP session context: a resource read without a valid API key returns a 401-equivalent error. The auth check is identical to the tool-level auth check (`_check_api_key` in `src/mcp/server.py`) and is applied at the handler entry point, before any cache lookup.

### Cache Policy

**In-memory LRU, per-process, 1000 entries, 300-second TTL, thread-safe.**

The cache is implemented as an `OrderedDict`-based LRU in `src/mcp/resources.py` protected by a `threading.Lock`. The design choices:

- **1000 entries**: covers approximately 10 Odoo versions × 30 frequently-accessed models × 3 resource kinds (model + 2 fields on average), leaving headroom for view and module resources. At ~10 KB per entry (a typical model markdown tree), 1000 entries peak at ~10 MB of heap — acceptable for any deployment that already runs Neo4j and pgvector in the same tier.

- **300-second TTL**: five minutes. Chosen to match the granularity of an interactive coding session (a developer typically stays on one problem for several minutes before switching context). Within a session, the developer sees consistent content; across sessions or after a reindex, the cache expires before the next cold start. This is short enough that a reindex run (which typically completes in 2–15 minutes for a full profile) propagates to the resource layer within one cache lifetime without requiring explicit invalidation.

- **LRU eviction**: when the cache reaches 1000 entries, the least-recently-used entry is evicted. This ensures the cache naturally converges on the hot-path entities (popular models like `sale.order`, `account.move`, `stock.picking`) without manual tuning.

- **Thread-safety via `threading.Lock`**: the FastAPI server runs multiple request-handler threads concurrently. The lock protects both the LRU ordering (`OrderedDict.move_to_end`) and the TTL check-and-replace sequence. The lock is held only for the duration of the cache read or write — the Neo4j query executes outside the lock to avoid blocking other threads during I/O.

- **No cross-worker sharing**: in a multi-worker deployment (e.g., `gunicorn --workers 4`), each worker process holds its own independent LRU cache. A resource read handled by worker A does not populate worker B's cache. This is a known limitation (same as the 60-second Postgres session cache staleness documented in ADR-0029). The observed impact is limited: in the typical 4-worker configuration, the cold-miss rate is at most 4x the single-worker rate for the first 300 seconds after a cache fill. For popular entities (the top 100 models), all workers converge to cached state within one request per worker, because popular entities are requested by multiple concurrent AI clients.

**Postgres-backed cache with `head_sha` invalidation is explicitly deferred to M12.** A Postgres table keyed on `(uri, head_sha)` would allow cross-worker cache coherence and automatic invalidation when the indexer updates a repo's `head_sha`. This would eliminate both the cross-worker staleness window and the residual 300-second lag between a reindex and cache expiry. It requires:
  - A new migration (`migrations/0006_resource_cache.sql`).
  - A `head_sha` lookup per resource read (one additional Postgres query on cold-path).
  - A background eviction job or `ON CONFLICT` UPSERT pattern to bound table growth.

The M11 schedule cannot absorb this without delaying the rest of Wave F. The M12 deferral is explicitly recorded here so the follow-up implementor has the design rationale and can add the Postgres cache transparently — the `src/mcp/resources.py` cache interface (`_cache_get`, `_cache_put`) is designed as a thin shim to make the swap mechanical.

### `resources/list` Discovery: Top-100 Popular per Version

The MCP `resources/list` endpoint must return a finite, ordered list of resource URIs that AI clients can browse. Returning all indexed entities is not viable: a full corpus at 7 indexed versions × 400 modules × 100 models = 280,000 entries would freeze MCP client UIs (Claude Desktop, Cursor's resource browser, VS Code MCP panel).

The discovery strategy implemented in `src/mcp/resources_index.py`:

1. **Query**: for each indexed `odoo_version`, find the top 100 models ordered by inbound `DEPENDS_ON` edge count (dependency count). Models with more inbound edges are depended on by more other modules — they are the "hub" models that developers consult most frequently (`account.move`, `sale.order`, `stock.picking`, `res.partner`, `product.template`, etc.).

2. **Result**: each entry in `resources/list` contains `{uri, mimeType, description}`. The `description` is a one-line summary: `"Model sale.order (Odoo 17.0) — 3 modules depend on this"` or equivalent.

3. **Cap**: the total response is bounded at 100 entries per version × (number of indexed versions). For 7 indexed versions this is at most 700 entries — a number that all current MCP client UIs handle without performance regression.

4. **Rationale for top-N by dependency count**: dependency count is the best available proxy for "which models will developers look up?" in a static graph. It correlates with:
   - Cross-module coupling (a model depended on by many modules is modified by many developers).
   - Documentation need (hub models tend to have complex field interaction and inheritance chains that benefit from resource pre-fetch).
   - LLM context value (hub models appear more frequently in AI coding session transcripts).

   An alternative ranking by field count was considered and rejected: a model with 80 fields but zero external dependents is unlikely to appear in a cross-module coding session.

5. **Cypher query pattern** (Neo4j 5.x):
   ```cypher
   MATCH (m:Model {odoo_version: $version})
   WITH m, COUNT { ()-[:DEPENDS_ON]->(m) } AS dep_count
   ORDER BY dep_count DESC, m.name ASC
   LIMIT 100
   RETURN m.name AS name, dep_count,
          "odoo://" + $version + "/model/" + m.name AS uri
   ```
   The deterministic tiebreak (`m.name ASC`) follows ADR-0013's mandate for ORDER BY determinism.

### Authentication

All resource handlers enforce the same API-key authentication as MCP tools. A request without a valid `X-API-Key` header (or without a valid session-level API key, depending on the MCP transport) returns an error before any cache lookup or Cypher query executes. The auth check reuses `_check_api_key` from `src/mcp/server.py` without duplication.

Anonymous resource browsing (`resources/list` without auth) is not supported in v0.5.x. The resource list reflects the indexed corpus for the authenticated tenant's profile, which is profile-scoped — returning it without auth would leak profile membership information.

---

## Consequences

### Positive

- **Eliminates cold round-trips for static content.** Popular entities accessed within a 300-second window skip the Neo4j query entirely. For a developer inspecting `sale.order` fields repeatedly during a single coding session, the resource cache delivers sub-millisecond responses after the first read.

- **Enables host pre-fetch.** MCP hosts that support resource pre-fetch (Claude Desktop, Cursor) can load the top-100 popular model resources into the context window at session start, giving the LLM access to odoo-semantic graph data without any tool calls. This is the Google Drive pattern applied to Odoo's graph.

- **Stable URI scheme for cross-tool linking.** The `odoo://` URI can appear in tool outputs, docstrings, and external documentation as a stable reference. A developer can bookmark `odoo://17.0/model/sale.order` and retrieve current content from any MCP client without re-discovering the composite key.

- **Discovery funnel via `resources/list`.** The top-100 popular model list gives new AI clients a curated entry point into the graph without requiring the LLM to know model names in advance. It functions as a "most important entities" index analogous to a database's `information_schema`.

- **MIME-native content negotiation.** Hosts that support MIME-aware resource rendering (e.g., syntax highlighting for `text/x-python`, XML folding for `text/xml`) can apply their native viewers without custom parsing.

### Negative

- **Cross-worker cache incoherence** (up to 300s per worker) in multi-worker deployments. A reindex that updates `sale.order` will not propagate to all workers simultaneously — each worker's cache independently expires. Operator mitigation: `--workers 1` for single-server deployments; Postgres-backed cache (deferred to M12) for multi-worker production.

- **No push invalidation on reindex.** When the indexer completes a full or incremental reindex, it does not signal the resource layer to flush affected entries. The resource cache is a passive TTL-based cache; stale content persists for up to 300 seconds after the indexer finishes. For development workflows (frequent reindex → inspect → reindex cycles), developers should wait 300 seconds or restart the server process to guarantee fresh content.

- **`resources/list` is version-aware, not profile-aware.** The top-100 query selects models from the global graph without filtering by the caller's active profile. This means the discovery list may include models from modules not in the caller's profile. The tool layer (e.g., `resolve_model`) applies profile filtering at query time, so actual content reads are profile-correct; only the discovery list is over-inclusive. This is a known gap addressed in M12 profile-scoped resource discovery.

- **Seven new resource handlers add maintenance surface.** Each handler must stay in sync with its corresponding `_resolve_*` or `_list_*` tool function in `src/mcp/server.py`. A future schema change (e.g., a new Neo4j node property) requires updating both. The shared Cypher query pattern (handlers import the same query functions as tools) mitigates drift but does not eliminate it.

---

## Alternatives Considered

### 1. Resources as a thin proxy over tool calls — rejected

Instead of new Cypher queries in `src/mcp/resources.py`, resource handlers could call the existing `_resolve_*` functions and return their text output as resource content. This would eliminate the maintenance dual-path problem.

Rejected because:
- Tool functions return formatted tree-text with next-step hints, deprecation banners, and pagination footers — content designed for LLM consumption inside tool responses, not for resource bytes. A resource reader (e.g., a UI component or a `@`-mention widget) would receive the tree decorations as raw text.
- The MIME-type mapping requires format-specific output (JSON for fields, XML for views, Python for methods) that the text-channel tool responses do not produce.
- Tool functions carry the overhead of hint formatting, `_render_capped` truncation, and `TreeBuilder` rendering. Resource handlers should be thinner; the Neo4j query + raw format is sufficient.

### 2. Single flat URI scheme `odoo://{version}/{composite-key}` — rejected

A single `path` parameter containing the full composite key as a URI-encoded string (e.g., `odoo://17.0/sale.order%40sale%4017.0`) would be simpler to parse server-side.

Rejected because:
- Opaque URI-encoded composite keys are hard for humans to read and hard for LLMs to construct from partial information. The hierarchical `kind/path` grammar keeps kind and path as independently meaningful segments.
- The anti-pattern identified in research ("synthetic opaque URI for composite key — Slack and Memory deliberately keep composite keys decomposed") applies here: `(model, module, version)` as separate URI segments is more debuggable than a percent-encoded bundle.
- The kind segment enables content negotiation: a client that sees `odoo://17.0/view/...` knows to expect XML before making the request.

### 3. Expose all indexed entities in `resources/list` — rejected

Returning every Model, Field, Method, View, Module, PatternExample, and Stylesheet URI in `resources/list` would give AI clients complete visibility of the indexed corpus without additional tool calls.

Rejected because:
- At 7 Odoo versions × 400 modules × 100+ models × 50+ fields = hundreds of thousands of URIs, the response size would exceed MCP protocol limits and freeze MCP client UIs (documented in the Context section above).
- Most entities are not relevant to most sessions. A developer working on `purchase` customisation does not benefit from having `pos_restaurant`'s stylesheet URIs in their context window.
- The top-100 popular model list provides a Pareto-effective discovery funnel: 80% of developer queries touch the top 20% of models by dependency count.

### 4. Redis-backed resource cache — rejected

A Redis layer would provide cross-worker cache coherence and `head_sha`-keyed invalidation (flush the cache for a specific repo's entities when the indexer updates its `head_sha`).

Rejected for the same reasons as ADR-0029's Redis rejection: adds a third required infrastructure component, raises the operational bar for self-hosted deployments, and is not needed for the single-worker case that covers the majority of self-hosted instances. The 300-second TTL is a sufficient v1 tradeoff. The Redis path remains available as a future alternative to Postgres-backed cache if operational constraints change.

### 5. `resources/list` filtered by active profile — deferred to M12

Filtering the discovery list by the caller's active profile (from ADR-0029 session state) would make the list precisely relevant to the caller's indexed corpus rather than the global graph.

Deferred because:
- Profile-aware `resources/list` requires a join between the profile's module list and the top-N query, which adds Cypher complexity and a Postgres round-trip for profile lookup.
- The over-inclusive list is a minor UX issue, not a correctness issue: content reads (`resources/read`) are already profile-correct via the tool-layer query filters.
- The M11 schedule targets the LRU cache and top-100 discovery as a working v1; profile filtering is a M12 quality-of-life improvement.

---

## References

- Internal design notes §Pattern 8 — MCP Resources primitive with stable URIs (Google Drive `gdrive:///fileId`, Postgres Anthropic-reference table schemas, Filesystem roots).
- `/home/tuan/.claude/plans/rippling-greeting-tulip.md` §5 Wave F — per-WI spec for F1–F5; Appendix B item #10 (resource cache = in-memory LRU v1, Postgres deferred to M11 — now updated to M12 per scope adjustment) and item #11 (top-100 popular per version, avoid 10k-entry UI blowup).
- `src/mcp/resources.py` — Wave F WI-F1 implementation: 7 `@mcp.resource()` handlers, LRU cache (`_cache_get`, `_cache_put`, `_lru_lock`), MIME type dispatch, `"auto"` version resolution.
- `src/mcp/resources_index.py` — Wave F WI-F2 implementation: `list_resources()` returning top-100 most-depended-on models per version, ordered by `COUNT { ()-[:DEPENDS_ON]->(m) } DESC` with `m.name ASC` tiebreak (ADR-0013 determinism).
- `src/mcp/server.py` — Wave F WI-F3: `register_resources(mcp)` call; 7 priority tool docstrings updated with `See also: odoo://{version}/...` URI templates.
- `tests/test_mcp_resources.py`, `tests/test_mcp_resource_cache.py`, `tests/test_mcp_resources_list.py` — Wave F WI-F4: handler correctness (7 kinds), thread-safety (50 parallel reads), auth-bypass (401 without key), LRU eviction at 1001st entry, TTL expiry, cross-worker isolation assumption, `resources/list` cap at 100 per version.
- `docs/adr/0007-incremental-indexer.md` — `head_sha` tracking; the M12 Postgres-backed cache will use `head_sha` from the `repos` table as its invalidation key.
- `docs/adr/0013-defined-in-ranking-heuristic.md` — deterministic `ORDER BY` tiebreak convention applied to `resources/list` Cypher query.
- `docs/adr/0023-tool-output-completeness.md` — English-only output policy; all resource content is English (tool output and raw source code).
- `docs/adr/0025-css-scss-indexing.md` — `:Stylesheet` node composite key `(file_path, module, odoo_version)` used by the `stylesheet` resource kind.
- `docs/adr/0026-rbac-key-ownership.md` — `is_admin` DB-sourced pattern; resource auth check reuses the same `_check_api_key` helper.
- `docs/adr/0029-implicit-session-context.md` — `resolve_version_v2` three-tier resolution order applied when `version="auto"` appears in a resource URI.
