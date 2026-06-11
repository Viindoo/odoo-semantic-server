# Changelog

All notable changes to Odoo Semantic MCP are documented here.

> **Release policy note (2026-06-02):** Git tags exist only for v0.2.0, v0.3.0, v0.5.0, and
> v0.13.0. Releases v0.11.0 and v0.11.1 were shipped without a tag; v0.13.1 was likewise
> shipped untagged. v0.12.x was skipped entirely (no code or release at that version).
> Historical `[Unreleased]` blocks that preceded an untagged release have been relabelled
> as `[Merged into vX.Y.Z]` in this file to preserve history without misleading the reader.
> **Going forward, every release should be tagged immediately after merge** (`git tag vX.Y.Z && git push --tags`).

## [Unreleased]

### Fixed — ORM tools hang on dense inheritance + lint_check false-green (#271 #273, ADR-0048)

Two production issues fixed in one wave (10 work items + PR #275 review-round-3 fixes). Tool count
**stays 25**. No Postgres migration. Behavior changes flagged below.

#### Root causes

- **#273:** Three-layered: (1) writer created K×(K-1) same-name INHERITS mesh (~256k edges/version
  17.0); (2) ORM read used VLP `*1..3` anchored on all-K copies + ORDER BY before LIMIT, forcing
  86M path enumeration; (3) no timeout at any level - 11 zombie transactions ran 19-24h on prod.
- **#271:** Token-overlap matcher structurally cannot fire on SQL injection rules (W8140/E8501) -
  security vocabulary never appears in violation code. Secondary bug: empty-index returned silent
  false-green instead of a disclosure warning.

#### Fixes

- **Writer (ADR-0048 D1):** same-name INHERITS writer W1 now requires `tip.is_definition=true`.
  Topology changes from K² to K×D (D=1 in practice). Post-pass reconciliation at end of each
  `index_profile` (hoisted from `index_repo` - once per version after all repos complete; cut cost
  by R×) fills cross-repo write-order gaps (idempotent, version-scoped).
- **ORM read (ADR-0048 D2/D3, r3 CRITICAL-1):** `_lookup_field` step-3 and `validate_relation`
  5-hop chain replaced with per-hop name-dedup shape. Two structural fixes over the first cut
  (empirically proven necessary - prod measured 12.6s..TIMEOUT on un-cleaned K² mesh):
  (1) prune same-name DURING expansion (`h1.name <> $mn`, `h2.name <> pn1`, ...) so the BFS
  never re-enters the mesh; (2) aggregate to a SINGLE ROW before each subsequent hop via flat
  OPTIONAL MATCH + `WITH collect(DISTINCT ...)` so each hop runs exactly once regardless of K.
  Measured on K=120 un-cleaned mesh: 443ms (_lookup_field) / 109ms (validate_relation MISMATCH).
  Depth-first semantics are now formal contract (nearest ancestor wins; alphabetical module tiebreak
  at same depth). Per-hop `unresolved` filter tightened deliberately. CALL subquery shape removed
  (also eliminates Neo4j 5.26 `CALL { WITH }` deprecation).
- **Timeouts (ADR-0048 D7):** `neo4j.Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)` wraps all
  5 ORM read call-sites (default 30s). `OrmQueryTimeout` exception surfaces structured English
  error, no Cypher leaked. Thread-held `threading.BoundedSemaphore(ORM_QUERY_MAX_CONCURRENCY)`
  (default 8) wraps 4 ORM tool wrappers via `offload_bounded` decorator; acquire/release run
  INSIDE the worker thread (slot tied to thread lifetime, not coroutine cancellation - prevents
  the #276 pool-drain pattern). Fast-reject `OrmOverloaded` returns a plain string (ADR-0023
  uniform posture, never `isError=true`). Cancel-path metrics increment in-thread even when
  coroutine is cancelled. `_validate_orm_env()` fail-fast at startup: `SystemExit` on
  `NEO4J_QUERY_TIMEOUT_SECONDS <= 0`, `ORM_QUERY_MAX_CONCURRENCY <= 0`, or
  `ORM_SLOT_ACQUIRE_TIMEOUT >= NEO4J_QUERY_TIMEOUT_SECONDS`. `ORM_QUERY_MAX_CONCURRENCY` and
  `ORM_SLOT_ACQUIRE_TIMEOUT` moved to `src/constants.py` (SSOT per ADR-0048 D7 amendment).
- **Lint data (#271):** `code_pattern` (regex string | null) added to `LintRuleInfo` and all 12
  `lint_rules_*.json` files. 178 total patterns (security group 100% covered). A
  `_apply_code_patterns_overlay` post-pass patches patterns onto ALL rules after the merge loop,
  ensuring static JSON patterns propagate even to live-parse rule winners (first-write-wins dedup
  order confirmed by a real-merge-order test that locks both the live-parse win AND the overlay
  supply). Cross-version consistency enforced (same rule_id = same pattern across all versions).
  W8140/E8501 tuple interpolation form `execute("... %s" % (val,))` now fires - the `(?!\()` lookahead
  that blocked it has been removed from all 12 files. W8178 multi-line false-positive fixed (requires
  `)` on the same line now).
- **Lint matcher (#271, ADR-0048 D9):** pattern-first per-line `re.search` (V0.5 hybrid). Each
  violation labeled `[pattern]` or `[fuzzy]`. Banner updated to "Hybrid matcher (V0.5)".
- **3-tier disclosure (ADR-0048 D9, r3 HIGH #1):**
  - Tier-1a: `rules == []` alone triggers hard "NOT a clean bill of health" (silent false-green
    eliminated).
  - Tier-1b (NEW): `rules present + curate_status is None` (crash between `write_lint_rules` and
    `write_spec_metadata` sessions, or pre-SpecMetadata index) runs the matcher AND prepends a
    distinct soft banner "curation status unknown ... results may be incomplete".
  - Tier-2: `curate_status == 'pending'` with rules present - soft "limited results" banner.
  - Tier-3: `curate_status == 'complete'` - normal output, no banner.
- **Cleanup script:** `ops/cleanup_same_name_inherits_mesh.cypher` - 2-step batched via an OUTER
  driving `MATCH` + `CALL { WITH <row> ... } IN TRANSACTIONS OF 10000 ROWS` (backfill MERGE + delete
  ~1.1M same-name non-definition edges). Full reindex does NOT auto-clean (writer is additive MERGE).
  Requires backup bundle (ADR-0018) before running. Note: OUTER tx of `CALL IN TRANSACTIONS` is
  subject to `db.transaction.timeout` (verified Neo4j 5.26.25); for a full ~1.1M-edge run raise or
  disable the timeout first (script header Option A). Cleanup is a post-deploy ops step (not a
  safety prerequisite - per-hop pruning makes new code safe on old data).
- **Batched repo delete (#273):** `delete_modules_scoped` now uses `CALL {} IN TRANSACTIONS OF
  10000 ROWS` for child + Module deletion (was a single transaction). Same outer-tx timeout caveat
  documented in its docstring.
- **Metrics:** `orm_query_timeout_total{tool}` + `orm_overloaded_total{tool}` counters added to
  `/metrics` Prometheus endpoint. Both increment in-thread regardless of coroutine cancellation.
- **`_validate_depends` SSOT fix (MED #5):** last inline copy of the ADR-0034 tenant-choke predicate
  replaced with `_scope_pred("mth")` call. All 5 orm.py read call-sites now use the shared helper.
- **ORM tool docstrings (HIGH #6):** depth-first shadowing sentence added to all 4 ORM tool
  docstrings visible to MCP clients.
- **`limit_concurrency` formula:** headroom formula now accounts for both `EMBEDDER_MAX_CONCURRENCY`
  and `ORM_QUERY_MAX_CONCURRENCY`.
- **CSP (hCaptcha):** allow `https://hcaptcha.com https://*.hcaptcha.com` in `connect-src`, `script-src`, and `frame-src` so the rotating hCaptcha asset subdomains (`*.w.hcaptcha.com`, e.g. `logo.png`) stop tripping CSP on `/signup`. Pinned subdomains break per hCaptcha's CSP docs. Updated in Astro middleware + nginx + Caddy templates; `test_csp_headers.py` + `test_nginx_csp_ga_sync.py` updated. `Caddyfile.example` brought to GA-origin parity with nginx (was missing `https://www.googletagmanager.com` in script-src and `https://www.google-analytics.com https://www.googletagmanager.com` in connect-src); drift-guard tests added for Caddy GA/hCaptcha origins and middleware hCaptcha wildcards.

#### Behavior changes (flagged)

- `resolve_orm_chain` field resolution: depth-first (nearest ancestor) instead of alphabetical
  across all paths. Differs only when same field name exists at different inheritance depths with
  different types.
- Per-hop unresolved filter: paths through `__unresolved__` intermediate nodes are no longer
  traversed (to be verified during Wave 0 ops, expected 0 such paths in production data - not yet
  counted at the time of this PR).
- Lint: banner wording "V0 fuzzy" to "Hybrid matcher (V0.5)"; violation lines include
  `[pattern]`/`[fuzzy]` label. Empty-index now shows warning instead of empty tree.
- `OrmOverloaded` now surfaces as a plain string (was `isError=true`); slot release is now tied to
  thread completion (was coroutine cancellation).

#### Environment variables added

- `NEO4J_QUERY_TIMEOUT_SECONDS` (default `30`) - per-ORM-query driver timeout (in `src/constants.py`).
- `ORM_QUERY_MAX_CONCURRENCY` (default `8`) - semaphore cap for ORM tools (in `src/constants.py`).
- `ORM_SLOT_ACQUIRE_TIMEOUT` (default `5`) - fast-reject timeout for slot acquisition (in `src/constants.py`).
  All three fail-fast at startup if out-of-range (SystemExit). See `docs/operations/timeouts.md`.

#### Ops notes (see deploy runbook)

- **Wave 0 (before code deploy):** `CALL dbms.setConfigValue('db.transaction.timeout','600s')` +
  persist in `neo4j.conf` (NOT 60s - indexer has long-running transactions). Kill 11 zombie
  transactions. Create 2 indexes: `Model(name, odoo_version)` + `Field(model, odoo_version)`.
- **After deploy:** run `ops/cleanup_same_name_inherits_mesh.cypher` off-peak (backup ADR-0018
  first); run `index-core` for all 12 versions to populate `code_pattern` on LintRule nodes.
- **Prod smoke:** `resolve_orm_chain("product.product","categ_id","17.0")` < 5s;
  `validate_relation("sale.order","partner_id","res.partner","17.0")` < 5s (MISMATCH case: TIMEOUT
  before this fix, must now return in < 5s);
  lint SQL injection snippet with `[pattern]` W8140 hit; `/metrics` shows 2 new counter families.
- No Postgres migration. Tool count stays **25**.

---

### Fixed — docs/site MCP tool-count drift (24 -> 25)

Follow-up to PR #266 (`profile_inspect`, tool 25): several current-state references still advertised "24 tools". Corrected to 25 across live-facing surfaces; immutable history (per-PR CHANGELOG/ADR-at-time notes, completed TASKS items) left untouched.

- **Real bug — `site/src/lib/tools-data.ts` was missing the 25th entry (`profile_inspect`).** `TOOL_COUNT=25` in `constants.ts` but the `TOOLS` array had only 24 rows, so the vitest drift-guard (`tools-data.test.ts`: `TOOLS.length === TOOL_COUNT`, sequential `01..TOOL_COUNT`) was red and the homepage + `/tools` grid (both data-driven from `TOOLS`) rendered only 24 cards. Added `profile_inspect` (group `superset`, num `25`); bumped `TOOL_GROUP_LABELS.superset` `(3)` -> `(4)`.
- **Site strings:** `plugins-data.ts` install highlights ("24 tools" -> "25 tools"); stale `tools-data.ts` / `tools.astro` / `tools-data.test.ts` comments.
- **Docs (current-state only):** `README.md` (active-work + Admin Settings summaries), `TASKS.md` pre-launch sign-off rows, `docs/deploy/pre-launch-checklist.md` (§6 header + tool-count history chain now ends at 25), `docs/deploy/runbooks/README.md`, `docs/adr/0046` `/ready` surface note. The `prod-smoke` runbook gained a `profile_inspect` (#25) smoke phase + sign-off row.
- **Test:** `tests/test_health_liveness_readiness.py` `_get_mcp_tool_count` mock 24 -> 25 (arbitrary liveness test data realigned to the live surface; the one value-asserting test now expects 25).

Tool count is **25** (unchanged surface — this only corrects stale advertising). No migration. Web/docs/test only.

### Added/Fixed — Batch #258-#265 + #254: output hygiene, profile introspection, EE-confusion, lexical fallback, security (PR #266)

Nine issues fixed in one batch PR. Tool count **24 -> 25** (`profile_inspect` added). No migration.

- **#260 + #259-chain — `profile_inspect` (tool 25, ADR-0028):** new `profile_inspect(name, method, odoo_version)` discriminator superset. `method='summary'` surfaces ancestor chain + direct children + deduped repos + inheritance-inclusive module count. `method='repos'` lists distinct repos across the full ancestor chain (deduped on `(url, branch)`). `method='modules'` returns a paginated module list (cap 50, `start_index` cursor) scoped to the profile with optional `repo=` filter. RBAC: ADR-0034 tenant choke preserved; `_effective_allowed` pre-check denies non-visible profiles. `get_children_profiles()` helper added to `src/db/repo_registry.py`. ADR-0028 amended.
- **#262-B — `model_inspect(method='extenders')` (ADR-0028):** adds a paginated "Extended by" list (cap 20, `start_index` cursor) to `model_inspect`. `method='summary'` truncated list and paginated total now share the same `NOT is_definition` predicate (no off-by-one). Single-page response discloses total ("Showing all N of N"). ADR-0028 amended.
- **#261 + #265-Obs4 — uniform raw-text serialization (ADR-0023):** `describe_module` was the last tool wired to dual-channel output (`output_schema=DescribeModuleOutput.model_json_schema()`). FastMCP validates `output_schema` before the not-found path returns, causing `Output validation error` for missing modules (#261). Fix: removed `DescribeModuleOutput` DTO, `_describe_module_structured` helper, and the `output_schema=` kwarg. All tools now emit plain-text tree only (`"output_schema": None`). ADR-0023 amended (§A).
  - **⚠️ BREAKING CHANGE (external MCP clients only) — `structuredContent` is now `null` for `-> str` tools.** Setting `"output_schema": None` in `READONLY_TOOL_KWARGS` suppresses FastMCP's auto-wrap, so every text-returning tool now emits `structuredContent: null` in its MCP response instead of the previous auto-wrapped `{"result": "<tree text>"}`. **Migration:** read the tool output from `content[0].text` (the canonical channel — always present), NOT from `structuredContent["result"]` (now `null`). **Impact is limited to external integrators** that parsed `structuredContent["result"]`: all in-repo consumers, Claude Code, and the skill agents already read `content[0].text`, so they are unaffected. The dedicated structured-output tools (`model_inspect` / `module_inspect` / `entity_lookup` discriminator field) are unchanged — they return `ToolResult` directly and never went through the auto-wrap.
- **#265-Obs3 — operator hints purged from agent output (ADR-0023):** shell/CLI command strings (`index-repo`, `python -m`) removed from all `-> str` MCP tool outputs (`find_override_point` null-signature branch, `session.py` ValueError, `inspect.py` no-modules branch, `server.py` list_available_versions hint). Replaced with agent-actionable references to other MCP tools (`list_available_versions()`, `list_available_profiles()`). Regression test parametrized over all three MCP files.
- **#262-A — `model_inspect` limit documented and enforced:** cap corrected (was undocumented as 200 in ADR text while code enforced 50). All caps now documented in docstrings and enforced server-side with `effective_limit = min(limit, cap)`.
- **#258 + #259-B — docstring correctness for `odoo_version='auto'` and profile inheritance:** `set_active_version`/`set_active_profile` docstrings now match runtime: `'auto'` is a valid sentinel that resolves to the session-pinned version. `profile_name` filter semantics clarified: "inheritance-resolved" (a module visible via ancestor profile IS visible to its descendants).
- **#263 — EE-confusion: OPL-1 Viindoo modules no longer mislabeled as Odoo Enterprise (ADR-0036):** `_edition_label` now checks `_FIRST_PARTY_EDITIONS = {"viindoo"}` before the license lookup, so `edition='viindoo'` yields a Viindoo label regardless of license field. EE-confusion warning (`_is_ee_by_edition`) gates only on `edition='enterprise'` (OEEL-1 modules), not on OPL-1. OPL-1 is the Odoo Proprietary License for third-party apps; it is NOT Odoo Enterprise. ADR-0036 amended.
- **#264 — `find_examples` lexical fallback when embedder is down (ADR-0047):** new `src/mcp/example_lexical.py` helper provides an entity-name-first ILIKE search across the embeddings table without hitting the embedder. When embedding fails, `find_examples` returns degraded-but-useful lexical results labeled `match: lexical`, or a structured zero-result banner, instead of `RuntimeError`. Tenant choke (ADR-0034) preserved in the fallback SQL. ADR-0047 amended.
- **#254 — `TestOsmReaderGrant*` tests skip when DB user lacks CREATEROLE:** `tests/conftest.py` helper `_ensure_osm_reader` now catches `psycopg2.errors.InsufficientPrivilege` and issues `pytest.skip(...)` with a clear reason, instead of erroring. 8 grant-related cases skip cleanly on unprivileged CI runners; they run and pass on PG16 with CREATEROLE. New unit-tier test (`tests/test_conftest_osm_reader_helper.py`) verifies both branches without a real DB.
- **Security (M6 / CWE-209) — exception-detail leak suppressed across all tools:** `{type(e).__name__}: {e}` interpolation removed from all agent-facing `-> str` handlers (`suggest_pattern`, `set_active_version`, `set_active_profile`, `list_available_profiles`, `find_style_override`, `resources.py` stylesheet handler). Full exception detail is now logged server-side (`logger.warning(..., exc_info=True)`); agents receive a generic actionable message. ADR-0023 amended (§B).
- **Ops — `ops/backfill_module_profile.cypher` STEP 2/3 made executable:** VERIFY and drill-down Cypher statements are now proper `;`-terminated executable statements (were comment fragments). Running the file end-to-end via `cypher-shell` now includes verification. `docs/deploy.md §3.6c` documents mandatory deploy ordering: writer deploy -> backfill -> off-peak `--full` reindex.
- **Dead code removed:** `DescribeModuleOutput` DTO (`dto.py`), `_describe_module_structured` helper, dead `_ensure_osm_reader` local defs in three migration test files (replaced by conftest helper).

**Deploy note (for existing indexed instances):** run `ops/backfill_module_profile.cypher` via `cypher-shell` after deploying the writer fix, then schedule an off-peak `--full` reindex per `docs/deploy.md §3.6c`. No Postgres migration required.

### Fixed — literal CSS selector / SCSS variable lookup returned 0 (#255, ADR-0047)

- **Root cause (HNSW post-filter recall collapse):** `find_style_override(".o_list_view")`
  and `find_examples(".o_list_view", chunk_types=["css","scss","less"])` returned **0**
  while a natural-language query with the same filter returned 3, with a healthy embedder.
  Both tools ran pure pgvector ANN over the HNSW index with `chunk_type` as a post-filter and
  no cosine threshold; the `INSTRUCT_NL_TO_CODE`-wrapped literal selector became an
  out-of-distribution vector, so 0 of the top-`ef_search` candidates survived the post-filter.
- **Fix — literal-first lookup:** a verbatim CSS token (selector / `$`/`@` variable / mixin) now
  runs a deterministic substring ILIKE **before** ANN, via the shared `_literal_style_lookup`
  helper. The column is routed by token shape (selectors → `entity_name`; variables → `content`,
  the only place the variable name lives). ANN backfills remaining slots; results merge + dedup on
  `(chunk_type, module, file_path, entity_name, chunk_idx)` with literal ranked above semantic
  (deterministic `LITERAL_RANK_FLOOR + (n-i)*eps` tiebreak in the `find_examples` rerank). The NL
  ANN path is unchanged (zero regression).
- **New pure helper `src/mcp/style_literal.py`** (`is_literal_token` / `literal_column` /
  `ilike_pattern`): selector-shape detection (`.`/`#`/`[`/`&`/combinator/compound + bare BEM idents),
  at-rule-keyword flood guard (`@media`/`@import`/… → not literal; LESS `@brand-primary` → literal),
  and LIKE-metacharacter escaping with `ESCAPE '\'`.
- **General HNSW mitigation (flag-gated):** `_set_iterative_scan` issues
  `SET LOCAL hnsw.iterative_scan='relaxed_order'` (pgvector ≥0.8) before each ANN execute, inside the
  existing `_rls_read_tx` transaction, gated by the new `HNSW_ITERATIVE_SCAN` constant (set `''` to
  revert). Accepted trade-off: minor non-exact ordering of filtered-semantic results.
- **Embedder-outage robustness:** a literal style query never fetches/embeds on the hot path, so
  literal lookups still serve when the embedder is down — fixed symmetrically in both sync bodies
  and both async wrappers (`find_style_override`, `find_examples`).
- **Docstring fix:** the fabricated `find_style_override` example (`css · selector:.o_list_view`,
  `score 0.87`, `Found 2`) is replaced with realistic output (css `entity_name` is raw, scss/less
  has the `selector:` prefix; `$o-brand-primary` resolves via its declaration in `content`).
- **Scope:** both `find_style_override` and `find_examples` (only when `chunk_types ⊆
  {css,scss,less}` and the query is literal-shaped). `pg_trgm` GIN index deferred (current rowset
  makes ILIKE sub-millisecond). **Tool count stays 24; no migration.** Tenant choke
  `profile_name = ANY(%s)` (ADR-0034) preserved. See [ADR-0047](docs/adr/0047-literal-first-style-lookup.md).
- **Out of scope (tracked separately):** the intermittent embedder timeout is GPU VRAM contention
  (one RTX 3050 8GB shared by the embedding model + the coder model) — an ops fix, not a code fix.

### Added — reveal auto-minted default API key once after signup (PR #256)

- **Onboarding key visibility (UX gap):** new password (verify-email) and OAuth signups
  auto-mint a free-plan API key, but the plaintext was discarded at the mint call sites
  (`signup.py`, `oauth.py` — the `_mint_default_api_key` return value was thrown away) so users
  never saw it and had to manually create a second key. `POST /api/auth/verify-email` and
  `POST /api/auth/oauth-login` now return the minted plaintext as `new_api_key` (`null` for
  returning users, already-keyed users, or mint failure — fail-closed, never a 500). It is carried
  to `/account/api-keys` and revealed once in the existing copy-once banner:
  - password flow → `sessionStorage['osm-new-api-key']` (forwarded SSR→client via `define:vars`);
  - OAuth server-side 302 → a short-lived JS-readable cookie `osm_new_key`
    (`Path=/account/api-keys`, `Max-Age=60`, **`SameSite=Lax` — NOT Strict**: Strict is dropped on
    the OAuth redirect hop, mirroring the session-cookie rationale in `app.py`). `buildOAuthCallbackResponse`
    switched to `Headers.append` so the session cookie is never overwritten.
  - New non-admin OAuth signups are routed to `/account/api-keys` (overriding a deep-link `?return=`)
    so the one-time key is never stranded; the reveal consumes both carriers once and only displays
    `osm_`-prefixed values.
- **By design / unchanged:** lazy-mint `GET /api/api-keys` stays metadata-only (an idempotent GET
  must not surface a one-time secret); plaintext is never persisted server-side. Web/Astro only —
  **tool count stays 24; no migration.**

### Fixed/Added - #251 per-session MCP pin keying + profile read path wired

- **#251 (correctness, concurrency):** the sticky session pin (ADR-0029) was keyed by
  `api_key_id` **alone**, so concurrent Claude Code sessions on one API key clobbered each
  other - `set_active_version`/`set_active_profile` (and any resolving `odoo_version='auto'` or
  `profile_name`-omitting call) raced last-write-wins across sessions. Fix: key the pin by the
  composite `(api_key_id, mcp_session_id)` (the `mcp-session-id` header is read at tool-body
  time, reliable since #248/#250), so each live MCP session gets its own pin; stdio / CLI /
  header-less callers fall back to the `_nosession` sentinel bucket (pre-#251 single-pin-per-key
  semantics). **Storage moves to in-memory** as the source of truth (bounded by
  `MCP_SESSION_PIN_MAX`, default 50000, oldest-by-`set_at` evict; 24h in-memory idle TTL); the
  `api_key_session_state` Postgres table is now **vestigial - no longer read or written, but kept
  (not dropped)**. Pins **reset on server restart** (the `mcp-session-id` is ephemeral) - clients
  re-run `set_active_*` or pass explicit versions.
- **#251 (profile read path wired, narrowing-only):** the previously-dead profile read path is
  now live - `resolve_profile_v2`'s pinned profile is injected at the top of `_scope` (Neo4j) and
  `_effective_allowed` (pgvector) when a tool omits `profile_name`, then **re-validated at read
  time** through the existing ADR-0034 tenant choke. Strictly narrowing-only: the pin can only
  shrink the visible set within `own ∪ shared`, an out-of-scope pin on a scoped tenant fail-closes
  to deny-all, and admin stays unrestricted. **No new per-key `allowed_profile_ids` authz column**
  - the larger profile-authz design stays deferred; this only un-defers reading the
  already-recorded convenience default through the already-existing gate. No migration; no client
  code change required; tool count stays **24**. ADR-0029 amended.

### Fixed — #248 session version/profile pin ignored over HTTP + #237 owner-facing job errors

- **#248 (correctness, prod bug):** over the stateful streamable-HTTP transport,
  `set_active_version('16.0')` returned success but a later `model_inspect(..., odoo_version='auto')`
  silently resolved to the **latest** indexed version (e.g. 19.0) — the sticky-session contract
  (ADR-0029) was a no-op. Root cause: `AuthMiddleware`'s `request.state.api_key_id` did not survive
  the BaseHTTPMiddleware↔session-manager↔`request_ctx` boundary, so the tool body read the
  `'default'` sentinel and the session DB write/read no-op'd. Fix: recover the numeric PK from the
  always-surviving `X-API-Key` header via the warm auth cache in
  `UsageLogMiddleware.on_call_tool`/`on_read_resource` (`_recover_identity_from_header`) — one
  source-repair that fixes the version pin, profile pin, `odoo://auto/...` resources, and the
  usage/audit/tenant-attribution call sites that were also mis-attributed to `'default'`.
  `set_active_version`/`set_active_profile` now emit **honest receipts**: success only when the row
  was persisted; a loud error on a skipped write over authenticated HTTP; a gentle no-op note on
  stdio/CLI (silent `.debug` skip → `.warning`). The `set_active_version` success receipt also drops
  the obsolete "calls that omit `odoo_version=` will resolve to this version" wording (omission is
  now a validation error after the ADR-0029 required-version amendment) — it teaches "pass
  `odoo_version='auto'` to reuse this pin" instead (closes the surface-description point raised on
  Viindoo/odoo-mcp-client#38). New tests `tests/test_mcp_session_header_fallback.py` (RED-able
  real-hook regression) + `tests/test_mcp_session_receipt_honesty.py`. No migration; tool count stays
  **24**. ADR-0029 amended.
  > **Note (pinned-stack non-repro):** the state-loss was confirmed live on production but does NOT
  > reproduce under the currently pinned mcp 1.27.0 / fastmcp 2.14.7 / starlette 1.0.0 stack locally
  > (`request.state.api_key_id` survives there). The header-fallback is therefore a robust
  > defense-in-depth recovery that activates exactly on the prod topology that loses state, and is a
  > no-op where state already propagates — so it is safe to ship regardless of stack.
- **#237 follow-up (web-UI UX, non-security):** `GET /api/jobs/{id}/status` now returns a **sanitized
  category summary** of `error_msg` to a non-admin job owner (in-scope) instead of `null`, so the
  self-service portal can show *why* an index failed without leaking server paths / repo URLs / stack
  traces. Fixed-category mapping (`sanitize_job_error`) never echoes raw text — unrecognised errors
  fall to a generic default, so it is exhaustive-by-construction against current and future error
  producers. Admin still receives the full raw `error_msg`; `pid` stays admin-only; out-of-scope
  still 404. J2/J9 updated to the new contract + new J10 (category mapping + raw-path/URL stripped +
  admin-sees-raw). No migration.

### Fixed/Added — Issues #236/#237/#238 + require explicit odoo_version (PR #241)

- **#238 (correctness):** `model_inspect(method='fields')` now flags `related=` and `readonly`
  on fields (and `required` in the list view). Stored-related fields are no longer misread as
  writable. New `:Field` properties `readonly`/`inverse`/`effective_readonly`
  (`src/indexer/writer_neo4j.py`, `parser_python.py`, `models.py`); detail view gains a
  `Readonly:` line. Pre-reindex nodes degrade gracefully (markers omitted, never a misleading
  `Readonly: No`).
- **#237 (security, IDOR):** `GET /api/jobs/{id}/status` is now tenant-scoped (resolve
  `profile_name → profiles.tenant_id`, `is_in_scope`, unified `404` no-oracle, `error_msg`/`pid`
  redacted for non-admin). Same-class sweep: `clone-status`, `core-symbol-counts`, admin-gate on
  `backup status/stream`, auth on `ssh-keys-list`. Regression guard test enforces scope review on
  new sensitive GET routes. No migration (`profiles.tenant_id` from m13_002).
- **#236 (dev/CI):** restore-upload 403 fixed — root cause was a `127.0.0.1` vs `localhost`
  Origin mismatch (Astro `allowedDomains` empty). `ASTRO_DEV_ORIGIN`→`allowedDomains` at build
  time; `checkOrigin` stays `true` (prod posture unchanged). `parseDevOrigin` extracted to a
  shared SSOT module. ADR-0019 amended.
- **Require explicit `odoo_version` (ADR-0029 amend):** 19 version-bearing MCP tools now make
  `odoo_version` a **required** parameter (omission → validation error), so a long-running LLM
  session can no longer silently fall back to the latest-indexed version. Session/bootstrap tools
  (`set_active_version`, `set_active_profile`, `list_available_*`) and `odoo://` resources keep
  sentinel behaviour. Mirrored in the `odoo-mcp-client` tool surface (Viindoo/odoo-mcp-client#35 —
  **merge in sync**). Tool count stays **24**.

> **⚠️ Deploy — Neo4j metadata backfill required (do NOT blindly run `--full`):**
> #238 adds `:Field` graph properties but does **not** change pgvector embeddings. To populate
> them on an already-indexed production DB, run a **Neo4j-only backfill that skips re-embed**:
> `python -m src.indexer index-repo --all --full --no-embed`. `--full` is an in-place `MERGE`
> upsert (NOT a wipe); incremental is **insufficient** (unchanged `head_sha` → skip). Running
> `--full` *without* `--no-embed` would needlessly re-embed all of pgvector (~a full day).
> **Operators MUST investigate production state first** (diff scope, embedding model/dim, running
> jobs, time estimate, backup) per
> [`docs/deploy/runbooks/graph-metadata-backfill.md`](docs/deploy/runbooks/graph-metadata-backfill.md).

### Added — Analytics app_setting + /api/site-config extension (PR #225)

- **`analytics.ga_measurement_id` setting** — new Tier-1 runtime setting (category `analytics`,
  default `""`) in `SETTINGS_CATALOGUE` (`src/settings_registry.py`). 29th catalogue entry
  (18th non-billing Tier-1 setting). Readable without auth for the site config endpoint.
- **`GET /api/site-config` extended to 5 fields** — now includes `ga_measurement_id` alongside
  the existing `paid_checkout_enabled`, `helpdesk_url`, `site_version`, and `signup_enabled`.
  The Astro `<Analytics>` island reads this field at SSR time to inject the GA snippet only
  when a measurement ID is configured (no hardcoded GA ID in source).
- Tool count stays **24** (web/settings layer only; no new MCP tools; no migration).

### Added — HTTP readiness probe /ready (PR #229)

- **`GET /ready`** — new HTTP readiness endpoint on the MCP port (`:8002`). Returns `200 OK`
  with `{"status": "ready", "embeddings_total": N, "embeddings_by_chunk_type": {...}}` when
  the server is ready to serve (Postgres reachable + at least one embedding row present).
  Response is cached for 60 seconds (avoids per-request `SELECT COUNT(*)` on a large table).
  Returns `503 Service Unavailable` when not ready (e.g. DB unreachable at startup).
  **`/ready` is NOT an MCP tool** — it is an HTTP-only probe; tool count stays **24**.
- **Distinction from `/health`:** `/health` is a pure liveness probe (no DB I/O, returns
  `{"status": "alive", "version": "..."}` immediately). `/ready` is the readiness probe
  that confirms the data plane is populated and operational. ADR-0046 §observability contract.
- **`embeddings_total` / `embeddings_by_chunk_type`** are now served from `/ready` (not
  `/health`). `/health` returns `null` for these fields until the first `/ready` hit (per
  ADR-0010 amendment in ADR-0046).
- nginx template (`docs/deploy/nginx-m8.conf`) updated to include the `location = /ready`
  block alongside the existing `/health` location. (R6 code-review wave.)

### Added / Changed — Developer-first landing redesign + /examples showcase (feat/landing-living-cartography, PR #232)

Tool count stays **24** (web/Astro layer only; no new MCP tools; no migration).

- **New `/examples` page + `ExamplesShowcase` island.** 5 before/after scenarios
  (`model_inspect`, `find_override_point`, `impact_analysis`, `find_deprecated_usage`,
  `check_module_exists`) — ungrounded hallucination vs graph-verified output + token cost.
  `examples-data.ts` is the SSOT (English-only, mirrors the MCP tool surface); FAQ JSON-LD
  + a static, deep-linkable scenario grid for SEO/no-JS. The landing `PromptSimulator`
  now sources the first 3 scenarios from the same SSOT (no duplicate data).
- **Developer-first repositioning.** Section 01 "Built for everyone shipping Odoo." →
  "The Odoo intelligence layer your AI was missing." (kicker "BUILT FOR DEVELOPERS").
  Hero adds a "hallucination tax" callout (`account.invoice.search` vs `account.move`,
  `customer_id` vs `partner_id`). `PersonaCards` re-tiered: Developer full-width spotlight
  (primary), Consultant + CEO/PM secondary, BA/Sales + Marketer compact referral path.
- **Art direction "Living Cartography" (dark-luxury).** Glass surfaces, aurora field,
  grain texture, gradient text, scroll-reveal in `global.css`; `tailwind.config.mjs` adds
  glow/lift shadows + motion keyframes. Honours `prefers-reduced-motion` + `<noscript>`.
- **Nav:** "Live demo" → "Examples"; all "See examples" links now route to `/examples`.
- **A11y / hygiene:** hero callout uses dark-surface red (WCAG AA on glass); deep-link
  `#hash` targets reveal immediately; removed orphaned `.btn-*` CSS; added a `/examples`
  browser smoke test.

### Fixed — m13_018 backfill O(n²) → O(n) keyset-by-PK (issue #230)

- **`migrations/m13_018_embedding_model_dim.sql` backfill was O(n²) (LOW–MED, ops/tech-debt):**
  the loop used `WHERE ctid IN (SELECT ctid FROM embeddings WHERE embedding_model IS NULL LIMIT 10000)`.
  The `IS NULL` predicate has no supporting index, so every batch was a full sequential scan past an
  ever-growing filled prefix → `O(n²/batch_size)` (32+ min on prod's 591k-row / 7.3 GB table). Fixed by
  range-batching over the `BIGSERIAL` primary key (`id >= lo AND id < lo + step`, step 10k) so each batch
  is a bounded PK index-range scan → `O(n)`. Per-batch `COMMIT` retained (bounds lock + WAL, independent
  of scan cost); `step` kept small so each COMMIT caps WAL/lock at ≤ step wide-vector rows. Backfill
  stays idempotent (`AND embedding_model IS NULL`).
- **No re-deploy needed:** prod m13_018 already applied and finished with the old loop; yoyo tracks by
  migration id (not file content), so editing the file does not re-run it on migrated instances. This is
  a forward-looking fix for fresh-install / restore / CI / copy-paste reuse.
- **Regression guard:** `tests/test_m13_018_embedding_model_dim.py::test_backfill_is_bounded_not_repeated_seqscan`
  asserts the backfill range-batches over the PK and that the O(n²) `SELECT ctid … IS NULL … LIMIT`
  signature does not reappear. Perf-note added to `docs/huong-dan-stack.md` (§8) as the reusable pattern.
- Tool count stays **24**; no schema/migration-number change.

### Fixed — Code-review wave (R6): diagnostics alive-status + runbook/nginx /ready alignment

- **`src/diagnostics.py` mcp_health check false-error (HIGH):** `/api/diagnose` was permanently
  reporting `mcp_health=error` even when the server was healthy. Root cause: check compared
  `health_status == "ok"` but `/health` now returns `status: "alive"` (pure liveness, ADR-0046
  PR #227). Fixed: accept both `"alive"` (new) and `"ok"` (legacy) so diagnostics is correct
  across deployed versions. Detail message updated to clarify liveness context.
- **`docs/deploy/runbooks/post-pr-ops.md`:** Precondition health check updated from
  `expect "healthy"` → `expect "alive"` (liveness); added a second command showing `/ready`
  for readiness + embeddings counts.
- **`docs/deploy/reindex-v8-v19-runbook.md`:** GAP1 verify command switched from
  `/health` to `/ready` (`embeddings_total` is `null` on `/health` until the first `/ready`
  hit; `/ready` runs the real `SELECT COUNT(*)`).
- **`docs/deploy/nginx.conf.example`:** Added `/ready` location block (readiness probe) alongside
  the existing `/health` (liveness); clarified comments distinguishing the two endpoints.

---

### Fixed / Added / Changed — Token-bounded embedding, provider abstraction, MCP anti-hang (#226 #227)

Tool count stays **24** (no new MCP tools; `/ready` is a new HTTP endpoint, not a tool).
**Migration required on deploy:** `m13_018_embedding_model_dim.sql` (after m13_017).

#### Fixed — #226: token-bounded chunking (ADR-0044)

- **Root cause:** the chunking layer (`_sliding`, `make_pattern_chunks`, view/JS/style chunk helpers)
  split text by character window only. Character count is not a reliable proxy for token count;
  large patterns and code-dense chunks could exceed the embedder model's context window
  (`EMBEDDER_NUM_CTX`, default 4096 tokens), producing truncated or erroneous vectors.
- **Token helpers (`estimate_tokens` / `split_by_token_budget`)** added to `src/indexer/embedder.py`
  (module-level, shared with the chunking layer). Cheap heuristic: `ceil(len(text) / chars_per_token)`.
  Deliberately conservative (low ratio = over-estimate = safe over-split direction).
- **`_sliding` token-aware:** after each char window is produced, `_token_split_window` further
  splits by `EMBEDDER_TOKEN_BUDGET` (default 3500 tokens) if needed. Same pattern applied in
  `make_pattern_chunks`, `make_view_chunks`, `make_js_chunks`, `make_style_chunks`.
- **MCP query cap (`_cap_query_text`):** user-supplied query/intent/selector strings capped to
  `EMBEDDER_TOKEN_BUDGET` tokens before embedding. Only the leading chunk is used for search.
- **Truncation choke-point (`_truncate_to_ctx`):** last-resort safety-net in `_BaseHttpEmbedder`
  clamps any text exceeding `num_ctx * chars_per_token` chars with a `WARNING` log. Never splits
  into extra vectors; `len(out) == len(texts)` invariant preserved.
- **Bug B — length guard in `_embed_one`:** if the backend returns a different number of vectors
  than input texts, `RuntimeError` is raised immediately (prevents silent chunk-to-vector misalignment
  in the `embeddings` table).
- **Resilient skip-log (`_embed_chunks_resilient`):** `write_module_embeddings` now uses this helper.
  Happy path: one batch embed call. On batch failure: degrade to per-chunk embedding; any chunk that
  fails individually is logged as `WARNING` and skipped. A single malformed chunk cannot abort the
  entire module write.

New env vars: `EMBEDDER_NUM_CTX` (default `4096`), `EMBEDDER_TOKEN_BUDGET` (default `3500`),
`EMBEDDER_CHARS_PER_TOKEN` (default `3.0`). See [ADR-0044](docs/adr/0044-token-bounded-embedding.md).

#### Added — Provider abstraction (ADR-0045)

- **`EmbedderClient` structural Protocol** — `model`, `dim`, `num_ctx`, `chars_per_token` read-only
  attrs + `embed()` / `embed_async()` methods. `@runtime_checkable` so tests can assert the contract.
- **`_BaseHttpEmbedder`** — shared batch / retry / timeout / observability machinery. Subclasses
  override only: `endpoint_path`, `query_instruction`, `_build_payload`, `_extract_vectors`.
- **`OpenAICompatEmbedder`** — new `/v1/embeddings` client (POST `{model, input}`, extract
  `data["data"][i]["embedding"]`). No INSTRUCT prefix (symmetric models). Covers OpenAI, Voyage AI,
  TEI, vLLM, LiteLLM.
- **`make_embedder(backend, **kwargs)` factory** — selects `Qwen3Embedder` (`ollama` / `qwen` /
  `qwen3`), `OpenAICompatEmbedder` (`openai` / `tei` / `voyage` / `vllm` / `litellm`), or
  `FakeEmbedder` (`fake` / `test`) based on `EMBEDDER_BACKEND` env var (default `ollama`).
- **`embedding_model` + `embedding_dim` columns** — migration `m13_018` adds two columns to the
  `embeddings` table; existing rows backfilled to `('qwen3-embedding-q5km', 1024)`. Writer stamps
  every new row with the live embedder's `model` and `dim` attributes. `ON CONFLICT DO UPDATE` also
  refreshes provenance on re-index.
- **Fail-fast dim mismatch guard (`src/db/embedding_guard.py`)** — `assert_dim_matches(conn, dim)`
  raises `EmbedderDimMismatch` if the configured dim differs from the stored dim. Called once per
  `write_module_embeddings` batch. Prevents silent cosine-similarity corruption across incompatible
  vector spaces. **Switching embedding dimension requires a full reindex.**
- `EMBEDDER_BACKEND` env var added (default `ollama`). See [ADR-0045](docs/adr/0045-embedding-provider-abstraction.md).

#### Fixed — #227: MCP embed concurrency + anti-hang (ADR-0046)

- **Root cause (production wedge ~11h):** FastMCP invokes `sync def` tool handlers directly on the
  asyncio event loop thread. The three query-embed tools called `embedder.embed()` (blocking HTTP via
  `httpx.Client`) synchronously, freezing the entire event loop — including `/health`. Evidence: TCP
  `Recv-Q` grew 113→147 during wedge; wedge duration ~11h exceeded the 1200s batch timeout by ~30x.
- **Async hot path:** `find_examples`, `suggest_pattern`, `find_style_override` converted to
  `async def` and embed via `embedder.embed_async()` (runs `embed()` in a worker thread via
  `asyncio.to_thread`). Event loop stays free during embed.
- **Short query timeout (30s):** `embed_async(read_timeout="query")` uses `TIMEOUT_EMBEDDER_READ_QUERY`
  (default 30s), separate from the 1200s batch timeout. A single hung query embed fails fast rather
  than blocking a user for 20 minutes.
- **`asyncio.Semaphore` cap (`EMBEDDER_MAX_CONCURRENCY`, default 4):** bounds concurrent in-flight
  embed requests. Semaphore constructed lazily on first use (must be inside the running event loop).
- **Fast rejection (`EmbedOverloaded`):** callers wait at most `EMBEDDER_SLOT_ACQUIRE_TIMEOUT_S`
  (default 5s) for a slot. On timeout: raise `EmbedOverloaded` — surfaced as an actionable overload
  message instead of an unbounded queue.
- **uvicorn `limit_concurrency`:** set to `EMBEDDER_MAX_CONCURRENCY * 16` at server startup. Beyond
  this ceiling, uvicorn returns HTTP 503 immediately (not queuing). Tunable via `MCP_LIMIT_CONCURRENCY`.
- **`/health` — pure liveness, no DB I/O:** removed all `SELECT COUNT(*)` and pool checkout from the
  liveness path. `/health` reads a module-level cache (populated by `/ready` hits) in O(1), pool-
  independent. The `embeddings_total` / `embeddings_by_chunk_type` fields are retained in the
  response body (backward compat) but are `null` until the first `/ready` hit.
- **`/ready` — readiness probe with 60s cache:** new HTTP endpoint (`GET /ready`) runs Neo4j +
  Postgres connectivity checks + the `SELECT COUNT(*)` scan. Results cached 60s in-memory
  (double-checked lock); a burst of readiness probes triggers at most one DB scan per TTL. Not an
  MCP tool; **tool count stays 24**.

New env vars: `EMBEDDER_MAX_CONCURRENCY` (default `4`), `EMBEDDER_TIMEOUT_READ_QUERY` (default `30`),
`EMBEDDER_SLOT_ACQUIRE_TIMEOUT` (default `5`), `MCP_LIMIT_CONCURRENCY` (default `EMBEDDER_MAX_CONCURRENCY * 16`).
See [ADR-0046](docs/adr/0046-mcp-embed-concurrency-anti-hang.md).

---

### Fixed / Added — Public data-driven site-config, waitlist fix, standalone benchmark, GA4 (feat/website-data-driven-launch)

Tool count stays **24** (web/Astro/settings layer only; no new MCP tools).
**No migration** — the one new setting is seeded by the idempotent settings bootstrap (`ON CONFLICT DO NOTHING`).

- **Fixed: Waitlist never hid on `/pricing` even with billing enabled (root-cause = wrong data source).** `pricing.astro` read `billing.paid_checkout_enabled` + `billing.polar_checkout_url_map` from `GET /api/admin/settings` — an **admin-only** endpoint. A logged-out visitor's request returned 401/403, so `paidCheckoutEnabled` stayed `false` forever and every paid plan fell back to "Join Waitlist". Flipping the flag could never reach the public. Now the page reads checkout state from the **public** `GET /api/site-config` (single fetch). Per-plan fallback preserved (plan with a checkout URL → "Subscribe"; plan without → "Join Waitlist"); the bottom waitlist form renders only when ≥1 paid plan still lacks a checkout URL.
- **`GET /api/site-config` is now the single public runtime-config point.** Extended response contract (still no-auth, reuses the 3-tier settings resolver with its 60s L1 LRU — no new cache): `{ helpdesk_url, site_version, paid_checkout_enabled: bool, checkout_url_map: {slug:url}, ga_measurement_id: str }`. Polar checkout URLs are public buy-links, safe to expose.
- **`analytics.ga_measurement_id` setting (29th catalogue entry, new `analytics` category).** Default `""` (analytics off until an admin sets a `G-XXXXXXXX` id). Admin-tunable, data-driven — no rebuild to change/disable.
- **GA4 with Consent Mode v2 cookie banner, fully runtime/data-driven.** `GoogleAnalytics.astro` resolves the measurement id **client-side** from `/api/site-config` at page load (NOT baked at build) so it works identically on prerendered pages (landing, `/benchmark`) and SSR pages, and needs no rebuild. Consent defaults to denied for all storage; `CookieConsentBanner.tsx` (React island) prompts only when GA is configured and writes `osm_analytics_consent` to localStorage, calling `gtag('consent','update')` on accept. CSP (`site/src/middleware.ts`) gains `https://www.googletagmanager.com` (script-src) + `https://www.google-analytics.com` + `https://www.googletagmanager.com` (connect-src); `test_csp_headers.py` updated.
- **Standalone `/benchmark` showcase page + 4-axis examples.** New `site/src/pages/benchmark.astro` (prerendered) renders 7 cases across the value axes **Accuracy (no hallucination) · Full codebase picture · Token savings · Speed**. `benchmark-data.json` schema gains `title` + `accuracy`/`completeness`/`speed` fields; every `with_mcp` token count is **live-measured** against the indexed graph via the odoo-semantic MCP tools (tiktoken `cl100k_base`), `without_mcp` is a documented methodology estimate. Nav "Benchmark" now points to `/benchmark`; landing `#benchmark` becomes a teaser linking to it; `/benchmarks` remains the methodology page.
- **i18n: English-only public surface.** Translated the 3 benchmark `query` strings (were Vietnamese, shown on the landing cards) and the comments in `site/src/lib/plugins-data.ts` to English.

### Added / Changed / Fixed — Launch prep: install MCP-first, SEO/brand, legal compliance, checkout consent (feat/launch-prep)

Tool count stays **24** (web/Astro/billing layer only; no new MCP tools).
**Migration required on deploy (after m13_016):** `m13_017_withdrawal_consent.sql`.

- **Install page is MCP-first.** `/install/` (static HTML) + homepage `InstallSnippets.astro` now lead with the core client plugin `odoo-semantic-mcp` as the primary 3-step path (marketplace → install → `/odoo-semantic-mcp:connect`); `odoo-semantic-skills` is promoted afterward as an optional free (MIT) advanced add-on. `plugins-data.ts` SSOT gains a primary `installMcp` alias. `OpenSourcePlugins.astro` repositions MCP as core connector, skills as add-on.
- **Brand convention (SSOT).** New `BRAND_FULL`/`BRAND_SHORT`/`BRAND_DEF` in `site/src/lib/constants.ts`. "Odoo Semantic MCP" (full name) is primary across titles/H1/legal/first-mention/footer; "OSM" is the shorthand. Fixed wordmark (full name no longer drops "MCP" on mobile), unified logo `alt`, repaired Admin/Account/Tenant sidebar lockups, footer now prints the product name + defines "OSM (Odoo Semantic MCP)" once.
- **SEO + AI-discovery.** Canonical tags + JSON-LD (`Organization` in BaseLayout, `SoftwareApplication` on homepage, `Product`/`Offer` on pricing) + OG/Twitter on auth pages. Data-driven sitemap via `@astrojs/sitemap` (replaces drift-prone static `sitemap.xml`; now includes `/tools`, `/bootstrap`, `/terms`, `/privacy`, `/refund`; excludes auth/admin/account/tenant). New `public/llms.txt`. `robots.txt` disallows `/admin/`, `/account/`, `/tenant/`. `/benchmarks` (was orphaned) now renders shared SiteHeader/SiteFooter; homepage gains "See all tools →" / "Full methodology →" cross-links.
- **Legal pages — B2B + B2C compliant (B2B + B2C compliant; CEO sign-off 2026-06-01, external counsel pass recommended post-launch).** `terms`/`privacy`/`refund` rewritten per dual legal review + EU CRD research: submitter represent-and-warrant + indemnity + notice-and-takedown (ADR-0036 D5), derivative metadata/embedding license grant, Polar Merchant-of-Record / seller-of-record disclosure, liability-cap statutory carve-outs, EU consumer-forum clause. Refund split into B2B (all-sales-final) / EEA-UK consumers (14-day withdrawal + **pro-rata** mid-period per CRD Art. 9/14(3)/16(a) — **not** absolute no-refund) / VN+other. Full data-processor list (hosting, email, OAuth, hCaptcha, Polar as independent controller). Vietnamese-language versions for VN consumers (Law 19/2023 Art. 23). Legal entity + contact + effective-date SSOT in `contact.ts` with graceful-degradation for unfilled placeholders.
- **CRD-compliant checkout consent (billing).** New buyer-type capture (business/consumer) + non-pre-ticked withdrawal-waiver checkbox (CRD Art. 22) at checkout (`/account/billing` pre-redirect, since Polar checkout is URL-map based); consumer-without-waiver is blocked, business path skips the waiver. Persisted via `m13_017` (`subscriptions.buyer_type` + `withdrawal_waiver_accepted_at`). Durable-medium confirmation email (`src/web_ui/email.py`, CRD Art. 7(3)/8(8)). New endpoints in `src/web_ui/routes/account.py`; `_billing-island.tsx` consent modal. 10 new postgres integration tests.

- **Legal entity + contacts filled; DRAFT removed (CEO-authorized).** Real Viindoo Technology Joint Stock Company details (business reg-no 0201994665, registered address, hotline), effective date 2026-06-01, and `support@`/`sales@`/`privacy@`/`legal@viindoo.com` are now in the `contact.ts` SSOT. Public-page emails render via a new `ObfuscatedEmail.astro` component (JS-assembled; no plaintext address or `mailto:` in the static HTML — anti-harvest). DRAFT badges removed from terms/privacy/refund on CEO sign-off.

> **Launch gate (runtime ops, not in this PR):** legal text is CEO-authorized (no external counsel review yet — a post-launch counsel pass is recommended; no-refund-absolute for B2C subscriptions stays unlawful under EU CRD, which is why the compliant pro-rata mechanism ships here). Enabling live paid sales is a production runtime step, not a code change: an admin must set `billing.paid_checkout_enabled=true` and configure `billing.polar_checkout_url_map` in Admin Settings, and complete Polar KYB. Self-hosted deploys re-point `repos.local_path` / set `Astro.site` as usual.

### Added / Changed / Fixed — Pricing UX, /tools page, helpdesk setting, plugin split (feat/site-pricing-ux, PR #223)

Tool count stays **24** (web/Astro layer only; no new MCP tools).
**Migrations required on deploy (after m13_014):** `m13_015_pricing_model.sql` + `m13_016_plan_min_seats.sql`.

- **Per-seat pricing data layer.** Two new migrations:
  - `m13_015_pricing_model.sql` — adds `plans.pricing_model TEXT CHECK IN ('flat','per_seat')` (default `'flat'`); seeds `pro` + `team` plans as `per_seat`.
  - `m13_016_plan_min_seats.sql` — adds `plans.min_seats INTEGER` (display SSOT); seeds `team.min_seats = 3` to match `billing.team_min_seats` enforcement default. Note: `plans.min_seats` = display SSOT (pricing page copy); `billing.team_min_seats` setting = enforcement SSOT at checkout — keep in sync manually.
  - `GET /api/plans` now returns `pricing_model` + `team_min_seats` + `min_seats` fields. Admin plan editor gains a `pricing_model` dropdown and `min_seats` input.
- **`support.helpdesk_url` setting (28th catalogue entry).** New `support.*` category in `src/settings_registry.py`. Default `""` (helpdesk link hidden when empty).
- **`GET /api/site-config` endpoint (public, no auth).** Returns `{helpdesk_url, site_version}` — the only two fields safe for anonymous exposure. Exempt from auth middleware (`src/web_ui/middleware.py`). Consumed by `SiteHeader` to render the helpdesk link.
- **`/tools` page** (`site/src/pages/tools.astro`) — new public route listing all 24 MCP tools + 7 resources, with links to the install page.
- **Shared `SiteHeader` + `SiteFooter` components** (`site/src/components/`) — unified header/footer for public marketing pages (landing, pricing, tools); replaces duplicated inline markup.
- **Auth footer mini** — condensed auth footer (sign-in / sign-up links) added to public-page footer.
- **Terminology: "calls/minute" in rate-limit copy.** Two FAQ entries updated; pricing tier cards use consistent "calls/min" abbreviation.
- **Plugin content split.** Plugin documentation separates `odoo-semantic-mcp` (server connection) from `odoo-semantic-skills` (skill routing); promo page highlights MIT license for the client plugin.
- **Fixed: billing double-provision race (advisory lock).** `src/billing/provisioning.py` wraps `provision_or_upgrade` in a session-level Postgres advisory lock keyed on `(ns, subscription_id)` — closes the scan-B double-provision race where two concurrent webhook events for the same subscription could both pass the `api_key_id IS NULL` check before either committed.
- **Fixed: `connect_timeout` hot-path.** Database connection timeout no longer blocks the MCP request hot-path on cold-start.
- Lint / ruff fixes (no behaviour change).

### Added — Admin Settings category: support (feat/site-pricing-ux, PR #223)

- Admin Settings UI adds a **Support** category exposing `support.helpdesk_url` (the 28th catalogue entry). Admins can set the helpdesk URL at runtime without redeploy; the public `GET /api/site-config` endpoint exposes it to anonymous visitors.

---

### Added / Changed / Security — Billing & admin follow-ups from PR #219 (fix/issue-220-billing-followups)

Resolves issue #220 (three follow-ups deferred from PR #219), shipped as one PR.
Tool count stays **24** (web-UI / test only; no MCP tool-surface change).

- **Security (#2) — step-up MFA on plan-assignment routes.** `PATCH
  /api/admin/api-keys/{key_id}/plan` and `PATCH /api/admin/users/{user_id}/plan`
  now require `require_admin_with_fresh_mfa` (was plain `require_admin`),
  matching entitlement grant/revoke/update and plan price/quota edits — assigning
  a paid plan is entitlement-sensitive. Frontend `_api-keys-overrides-island.tsx`
  and `users.astro` wrap the plan fetch in `withStepUp(...)` so a stale-MFA admin
  gets the step-up modal instead of a dead 403. Guarded by FastAPI
  dependency-tree introspection tests (fail if the dependency is downgraded).
- **Admin UX (#1) — `/admin/entitlements` CRUD UI.** New Astro page +
  `SubscriptionsTable.astro` + `_entitlements-island.tsx` for the existing
  Entitlement Activation API (list / grant / revoke / update); all mutations go
  through `withStepUp` (backend requires fresh MFA). Adds the `Entitlements` nav
  entry (`AdminLayout`) and an admin-only middleware guard for
  `/admin/entitlements*`. No backend route changes.
- **Maintainability (#3) — single source of truth for tool/resource count.**
  New `site/src/lib/constants.ts` (`TOOL_COUNT`, `RESOURCE_COUNT`); the six
  hardcoded "24 tools / 7 resources" strings on the marketing pages (Hero,
  InstallSnippets, index, pricing) now import it. `tests/test_tool_count_sync.py`
  asserts the constants match the live MCP surface (`mcp._tool_manager._tools`,
  `mcp._resource_manager._templates`) so drift fails CI. Landing page stays
  static (`prerender = true`); no `/health` / SSR change.

### Added — M10B P1 billing: Entitlement Activation API + Polar webhook + claim-on-login (feat/m10b-p1-billing)

- **Migration `m13_014_billing_p1.sql`** (required on deploy). Three schema additions, all
  idempotent (`IF NOT EXISTS` + guarded `DO` blocks):
  - `plans` gains commercial pricing columns: `price_cents` **BIGINT** (upgraded from INTEGER;
    VND whole-units can exceed INT4 2.1B max), `currency` (with ISO-3-letter `CHECK ~ '^[A-Z]{3}$'`),
    `billing_interval` (CHECK: `free/monthly/annual/one_time`), `trial_days`, `is_archived`.
  - `subscriptions` table — commercial-only, integer FKs (`plan_id→plans`,
    `claimed_user_id→webui_users`, `api_key_id→api_keys`, `tenant_id→tenants`),
    `buyer_email` snapshot (claim-on-login anchor), `UNIQUE(source, external_ref)` composite key
    (vendor idempotency key; composite so the same Polar order ID can appear across future vendors
    without collision), `currency` (with ISO-3-letter `CHECK ~ '^[A-Z]{3}$'`),
    `amount_cents` **BIGINT**, `last_event_at TIMESTAMPTZ` (monotonic guard — out-of-order
    webhook events are dropped when their timestamp is older than the stored value),
    status/seats/source/money-snapshot/timeline columns. NO per-row limit columns —
    limits live only in `plans`, resolved via `plan_id` at runtime.
  - `billing_webhook_events` idempotency ledger — `(vendor, event_id)` UNIQUE; every webhook
    attempt recorded with `signature_valid` flag, `processed_at`, `processing_error`.
  - `osm_reader` SELECT grants on both new tables (in-migration, pg_roles-guarded).
- **Pricing seed (in-migration, idempotent):** Free quota bumped 100 → 200 calls/month; plan
  pricing set: Free $0, Pro $19/seat/month, Team $39/seat/month.
- **Vendor-agnostic Entitlement Activation API** (`src/billing/activation.py`,
  `src/db/subscription_registry.py`). `EntitlementGrant` frozen dataclass uses integer `plan_id`
  (never a text slug). `grant_entitlement` / `update_entitlement` / `revoke_entitlement` are the
  sole writers of subscription state; both the admin API and the Polar webhook call through them.
  On revoke/cancel: linked API key downgraded to `free` + middleware plan cache flushed immediately
  via `_cache_invalidate_by_key_id`.
- **Admin Activation API** (`src/web_ui/routes/entitlements.py`):
  - `POST /api/admin/entitlements` — grant an entitlement (resolve plan_id from slug server-side,
    `@audit_action("entitlement.grant")`).
  - `POST /api/admin/entitlements/{external_ref}/revoke` — cancel + downgrade.
  - `PATCH /api/admin/entitlements/{external_ref}` — update plan/status/seats/period.
  - `GET /api/admin/entitlements` — list / search subscriptions.
  - Mutating routes (`POST` grant, `POST` revoke, `PATCH` update) require
    `require_admin_with_fresh_mfa` (DB-sourced + MFA step-up, ADR-0026/ADR-0043). Read-only
    `GET` (list) uses plain `require_admin`. All mutating routes carry `@audit_action` (ADR-0021).
- **Polar.sh webhook sink** (`src/web_ui/routes/webhooks.py`):
  - `POST /api/webhooks/polar` — public route (auth-exempt via `_EXEMPT_EXACT`), HMAC-verified
    using Standard Webhooks spec (base64 HMAC-SHA256 over `"{id}.{timestamp}.{body}"`; `whsec_`
    prefix stripped + base64-decoded). **Fail-closed:** missing `POLAR_WEBHOOK_SECRET` → 503.
    Idempotent: `billing_webhook_events (vendor, event_id)` UNIQUE deduplicates Polar retries.
    Bad signature → 400 + ledger row `signature_valid=FALSE`, not processed. Per-IP rate-limit
    via `billing.webhook_rate_limit_rpm` app_setting.
  - Product→plan resolution via `billing.polar_product_map` (JSON app_setting, hot-reload ≤60s).
  - Handled events: `subscription.created/active/updated/canceled/revoked`, `order.paid/refunded`.
  - **`recurring_interval` dual-path extraction:** reads `data.recurring_interval` first; falls back
    to `data.price.recurring_interval` for older Polar payloads that nested it in the price object.
    `day`/`week` tokens are normalised to `monthly` (safe fallback — no day/week product sold today).
    `null` → `one_time`. Mapping: `month→monthly`, `year→annual`.
  - **Status normalisation:** Polar `unpaid` maps to `expired` (definitive payment failure);
    `ended`/`incomplete_expired` also map to `expired`.
  - **Transient-vs-permanent error routing (money-safety):** `IntegrityError` / `CheckViolation` /
    `ValueError` (bad data that will never succeed) → mark event processed + return **200** so Polar
    stops retrying a poison event (permanent). `OperationalError` / DB pool timeout / any other
    exception → do NOT mark processed + return **5xx** so Polar retries later (transient). Failed
    events are always recorded in the ledger `processing_error` for ops investigation.
  - **Self-heal / reprocess:** a webhook event that was NOT previously marked processed (crash
    mid-flight) is re-dispatched on the next delivery attempt; already-processed events are
    deduped and return 200 immediately.
- **Public `GET /api/plans`** — returns active (non-archived) plans with new pricing columns for
  the pricing page; no auth required.
- **Claim-on-login provisioning** (`src/billing/provisioning.py`). `claim_subscription_for_user(
  user_id, email)` runs best-effort (never raises into auth) at three verified-email call sites:
  email-verify (`routes/signup.py`), OAuth login (`routes/oauth.py`), password login
  (`routes/login.py`, only when `email_verified=TRUE`). Finds unclaimed active subscriptions for
  the buyer email, upgrades the user's existing free API key in-place, links subscription to user
  + key, flushes plan cache.
- **3 new `billing.*` Tier-1 settings** in `src/settings_registry.py` (→ 19 Tier-1 settings
  total): `billing.polar_product_map` (struct, default `{}`), `billing.webhook_tolerance_seconds`
  (int, default 300), `billing.webhook_rate_limit_rpm` (int, default 120).
- **`POLAR_WEBHOOK_SECRET`** added to `src/web_ui/config.py` (env var, fail-closed → 503 if
  absent when the webhook route is called).

> **Tool count stays 24.** All billing changes are web-UI / webhook layer only. No new MCP tools.
> **Migration m13_014 required on deploy.** Set `POLAR_WEBHOOK_SECRET` in `webui.env` / systemd
> BEFORE the webhook route goes live. Set `billing.polar_product_map` in Admin Settings
> post-deploy. Re-run `ops/rls_create_osm_reader.sql` if not relying on the in-migration grant.
> **FLAG:** Polar webhook header names / `whsec_` encoding / event-type spellings / payload field
> paths must be confirmed against live Polar docs before production (constants in
> `src/billing/polar.py`).

### Added — M10B P1 billing completion: schema hardening + self-serve cancel + admin config + legal + dashboard (feat/m10b-p1-billing W1-W6)

- **Migration `m13_014_billing_p1.sql` extended** — all W1 schema additions are now gộp vào
  m13_014 (single migration for the entire billing schema, easier deploy + review). Sections 6-8
  add the following on top of the original P1 schema (sections 1-5):
  - **Section 6 — cancel_at_period_end + per-currency prices (formerly m13_015)** (idempotent):
    - `subscriptions.cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE` — UI/state signal for
      voluntary cancel-at-period-end; actual period-end downgrade driven by the Polar
      `subscription.canceled` webhook.
    - `plans.prices JSONB NOT NULL DEFAULT '{}'` — per-currency price map (additive alongside
      scalar `price_cents/currency`). Example: `{"USD": 1900}`. Seeded (guarded — only when
      `prices='{}'`): Pro `{"USD":1900}`, Team `{"USD":3900}`, Free/Unlimited `{"USD":0}`.
      **Multi-currency display deferred to P2;** VND key removed from seed (the `prices` JSONB
      column is designed to hold future currencies — add a VND key when a VND pricing tier is
      decided).
  - **Section 7 — signup consent (formerly m13_016)** (idempotent):
    `webui_users.terms_accepted_at TIMESTAMPTZ` — auditable proof-of-consent. `NULL` = legacy
    (grandfathered). Non-NULL = timestamp of checkbox acceptance at signup (password or OAuth).
    Required by PDPL 91/2025 + card-network requirements.
  - **Section 8 — drop waitlist plan CHECK (formerly m13_017 draft; file number reused by PR #224 for CRD consent — see [Unreleased] entry above)** (idempotent):
    Drops the hard-coded `CHECK (plan IN ('free','pro','team'))` from `waitlist_emails` (m13_008
    artefact). Waitlist plan validation is now DB-derived (`_public_plan_slugs` queries
    `plans WHERE is_public=TRUE AND is_archived=FALSE`). No replacement constraint.
- **Vendor-generic webhook pipeline** (`src/billing/webhook_pipeline.py`):
  `WebhookAdapter` frozen dataclass + `run_webhook_pipeline` function encapsulate the full
  13-step processing order (rate-limit, fail-closed secret check, signature verify, ledger
  record, dedup, event-action map, plan resolution, grant/update/revoke dispatch,
  mark-processed) in vendor-agnostic code. The Polar handler is the first adapter. A second
  vendor (Paddle/ERP) is ~25 lines of glue + a route — no pipeline duplication.
- **Vendor-neutral slug helper** (`src/billing/_db.py`): `slug_to_plan_id(slug, conn)` —
  fully parameterised `plans.id` resolver used by all adapters. No SQL injection vector.
- **`src/billing/__init__.py` re-exports cleaned** to vendor-neutral surface only; vendor
  adapters imported namespaced.
- **Self-service cancel-at-period-end** (owner decision: no refund, access to period end):
  - `src/billing/polar_api.py` — outbound Polar REST client (`httpx`, `POLAR_API_KEY`,
    `billing.polar_api_base`). Fail-closed: absent key → `PolarApiNotConfigured` (HTTP 503 +
    portal URL); non-2xx / transport → `PolarApiError` (HTTP 502). Cancel path:
    `PATCH {base}/v1/subscriptions/{id}` with `{"cancel_at_period_end": true}`.
    **FLAG: confirm endpoint + payload against live Polar docs before go-live.**
  - `activation.revoke_entitlement(voluntary=True)` — schedules `cancel_at_period_end`;
    leaves `status='active'`; does NOT downgrade key. `voluntary=False` (default) → immediate
    downgrade (unchanged).
  - `GET /api/account/subscription` — returns active subscriptions with `plan_slug`,
    `plan_name`, `cancel_at_period_end`, `current_period_end`, `manage_url` (Polar portal).
  - `POST /api/account/subscription/cancel` (`@audit_action`) — calls Polar API first; local
    flag set ONLY on Polar success. 503 + `portal_url` when `POLAR_API_KEY` absent; 502 on
    Polar error (local flag not set, no false "cancelled" confirmation).
- **Admin plan price editing**: `PATCH /api/admin/plans/{slug}` (`PlanPatch`) now accepts
  `price_cents`, `currency`, `billing_interval`, `trial_days`, `prices` (per-currency map),
  and `is_archived`.
- **8 new `billing.*` Tier-1 settings** in `src/settings_registry.py` (total billing settings:
  11; total catalogue entries: 28 — including `support.helpdesk_url` added in PR #223):
  `billing.free_plan_slug` (default `"free"`),
  `billing.unlimited_sentinel_slug` (default `"unlimited"`),
  `billing.team_plan_slug` (default `"team"`),
  `billing.team_min_seats` (default `3` — **enforced** at `grant_entitlement`; `ValueError` →
  HTTP 422 on admin API; webhook records in ledger `processing_error`),
  `billing.polar_portal_url` (default `"https://polar.sh/"`),
  `billing.polar_api_base` (default `"https://api.polar.sh"`),
  `billing.paid_checkout_enabled` (default `False` — gates paid CTA on `/pricing` + legal pages),
  `billing.polar_checkout_url_map` (default `{}`).
- **Legal pages** (`/terms`, `/refund`, `/privacy`) — Astro static pages with DRAFT badge.
  Stance: no-refund + cancel-at-period-end. All three pages marked "DRAFT — pending legal
  review"; `paid_checkout_enabled` was to remain `False` until legal sign-off + KYB complete.
  **(Update: legal sign-off done per PR #224 (DRAFT removed 2026-06-01); the flag was subsequently
  flipped to `true` in prod (2026-06) — Polar KYB + cancel-endpoint/webhook confirmation still pending.)**
  Footer links to all three pages.
- **Required signup consent checkbox** — disables submit until checked (client-side guard).
  Backend records `terms_accepted_at = NOW()` in `webui_users` for both password signup
  (`routes/signup.py`) and OAuth account-creation (`routes/oauth.py`).
- **`/account/billing` dashboard page** — auth-gated Astro page + `BillingDashboard` React
  island. Displays plan name, status, seats, renewal/period-end date, `cancel_at_period_end`
  state, Polar portal link, and a cancel button (`POST /api/account/subscription/cancel`).
- **`/pricing` data-driven** (`prerender=false`) — fetches `GET /api/plans` at SSR time for
  live prices (USD per `plans.prices`; multi-currency display deferred to P2). Checkout CTA
  gated by `billing.paid_checkout_enabled`. Usage counter auto-refreshes every 60s.

> **Tool count stays 24.** All W1-W6 completion changes are schema / web-UI / webhook /
> Astro layer only. No new MCP tools.
> **Migration m13_014 is the single migration required for M10B P1 billing** — it covers all
> billing schema (W1 schema additions are gộp vào m13_014; the previously separate draft files
> m13_015/m13_016/m13_017 were merged into m13_014 and do not exist as separate files for this PR).
> **[SUPERSEDED]** m13_015/m13_016 were subsequently re-created by PR #223 (`plans.pricing_model`
> and `plans.min_seats`), and m13_017 was re-created by PR #224 (CRD withdrawal consent:
> `subscriptions.buyer_type` + `withdrawal_waiver_accepted_at`). All three files now exist in the
> repo and are applied in prod. See PR #223 / PR #224 entries below.
> (m13_017 file number subsequently reused by PR #224 for CRD withdrawal consent — see [Unreleased] entry above)
> **PR #223 adds NEW migrations m13_015 (`plans.pricing_model`) and m13_016 (`plans.min_seats`)**
> using the now-available file numbers. Deploy order: m13_014 → m13_015 → m13_016.
> **PR #224 adds NEW migration m13_017 (`subscriptions.buyer_type` + `withdrawal_waiver_accepted_at`).**
> Full deploy order: m13_014 → m13_015 → m13_016 → m13_017.
> Set `POLAR_API_KEY` in `webui.env` / systemd for the self-service cancel route.
> **Legal pages CEO-signed (DRAFT removed, PR #224, 2026-06-01).** External counsel review
> recommended post-launch. Enabling live paid sales: admin set `billing.paid_checkout_enabled=true`
> + configure `billing.polar_checkout_url_map` after Polar KYB.
> **FLAG:** Polar cancel endpoint/payload must be confirmed against live Polar docs; constants
> in `src/billing/polar_api.py` and `src/billing/polar.py`.

### Added — OAuth deep-link return + avatar dropdown + account UX (feat/webui-oauth-avatar-uiux)

- **OAuth `?return=` deep-link threading.** Google/GitHub callbacks now honour a
  `?return=<path>` query param on `/login` and `/signup`. The path is stored in a
  single-use `oauth_return` cookie (strict same-origin safe-path validation — no open
  redirect), consumed by the callback and discarded. Closes deferred item from PR #214.
- **Avatar user-menu dropdown (all 3 layouts).** Authenticated header now shows a
  user avatar button that opens a consolidated personal-actions menu: API Keys,
  Repositories, Usage, Security/2FA, Change Password; role-gated entries for Admin
  Dashboard + Admin Settings (admin only) and Tenant Settings (tenant_admin only);
  Logout at the bottom. Logout removed from bottom-left sidebar across all 3 layouts.
- **In-session change-password page (`/account/change-password`).** New page + React
  island + `POST /api/auth/change-password` endpoint (requires valid session, enforces
  `auth.password_min_length` + common-pw blocklist, re-auths session on success).
- **Account-scoped 2FA page (`/account/security`).** Dedicated page for TOTP
  enroll/disable + backup-code regeneration, accessible to all authenticated users
  (previously only reachable deep in the admin settings flow).
- **Mobile sidebar drawer (hamburger toggle)** added to admin, account, and tenant
  layouts. Sidebar collapses to a hidden drawer on small screens and slides in/out via
  hamburger button; JS-free CSS approach, zero dependency.
- **Skip-to-content links** added to all 3 app layouts and the public `BaseLayout` for
  keyboard/screen-reader navigation (WCAG 2.4.1).
- **`viindoo-danger` colour token** (`#C0331F` / Tailwind `bg-viindoo-danger`) added to
  the design-token layer for destructive-action buttons and error states.
- **`/bootstrap` discoverability links.** Persona dev card now links to the install page;
  install page links back to `/bootstrap`. Increases organic discovery for self-hosted
  admins landing on the bootstrap page.
- **`/api/auth/verify` now returns `email` + `is_tenant_admin`.** Extends the verify
  response payload so Astro SSR middleware and client islands can surface user-specific
  UI without an extra round-trip. Tool count stays **24** (web-UI only; no new MCP
  tools).

### Changed — OAuth deep-link return + avatar dropdown + account UX (feat/webui-oauth-avatar-uiux)

- **Logout moved from bottom-left sidebar into the avatar dropdown** across admin,
  account, and tenant layouts (consistent with the consolidated personal-actions menu
  described above).
- **Pricing page reconciled to the single unified Free plan.** Removed obsolete
  grandfathered-tier copy left from the pre-PR-#214 plan structure; page now matches the
  live `free` plan limits (100 calls/month, 30 rpm).
- **Footer/landing/install "GitHub" links now point to the public `odoo-mcp-client`
  repo** with a "Contribute" CTA. The server repo (`odoo-semantic-server`) is going
  private; client tooling repo remains public and is the right destination for
  community-facing links.
- **Nav emoji icons replaced with SVGs** for all sidebar nav items and the header
  branding mark. Eliminates font-fallback rendering differences across OS/browser.
- **Version string bumped to v0.13.1** in `site/src/lib/constants.ts` (`SITE_VERSION`) and the FastAPI
  app version header.

### Fixed — OAuth deep-link return + avatar dropdown + account UX (feat/webui-oauth-avatar-uiux)

- **WCAG-AA contrast on persona cards + repo-table focus rings + footer meta.** Persona
  cards previously failed SC 1.4.3 (contrast < 4.5:1 on body copy). Focus rings on the
  repo table action buttons did not meet SC 1.4.11 (UI components, 3:1). Footer
  meta-links used `gray-400` on dark — now `gray-300`. Lighthouse accessibility score
  reaches **100 on the landing page** post-fix.
- **`<main>` landmark added to public `BaseLayout`.** Previously absent, causing
  screen-reader users to have no main-content landmark on landing, pricing, and
  bootstrap pages (WCAG 1.3.1 / technique H69).
- **`forgot-password` already-authed redirect is now role-aware.** Authenticated admins
  who landed on `/forgot-password` were double-bounced (admin → `/login` → admin). Now
  redirected directly to the correct role destination via `auth-landing.ts`.
- **Toast/flash banners announce via `role=status` / `aria-live="polite"`.** Previously
  silent to AT; status messages are now surfaced to screen readers without interrupting
  the reading flow.
- **Login button casing consistency.** "LOGIN" / "Log In" / "Sign In" variants
  standardised to "Sign in" across all auth pages.

> **Tool count stays 24.** All changes in this PR are web-UI/auth layer only. No new
> MCP tools, no database migration.

### Changed — Free-plan consolidation + auto-onboarding (fix/auth-ux-oauth-cache-plans)

- **Admin/CLI keys moved to `unlimited` plan; `free-grandfathered` plan deleted.** Migration `m13_013_consolidate_free_plans.sql`
  repoints all `free-grandfathered` API keys (6 internal/admin/CLI keys) to the `unlimited` plan
  (ADR-0041 D5 SSOT), then deletes the legacy `free-grandfathered` plan row. New signups continue to
  land on the public `free` plan (100 calls/month, 30 rpm).
- **Auto-onboarding for new signups:** Both password + OAuth signups auto-assign the `free` plan and
  auto-mint one API key (auto-generated name `auto_{user_id}_{timestamp}`). Landing post-login points
  users to `/account/api-keys` to see their key. Closes onboarding friction gap.
- **OAuth session cookie SameSite: Strict → Lax.** Fixes Google sign-in on Windows IE-compat cookie
  handling — same-site Strict blocks third-party-initiated cross-site POST redirects (Google's callback
  to `POST /admin/auth/google/callback`). Changed to Lax per OWASP: allows top-level navigations,
  blocks auto-submitted forms. Backward-compatible with existing sessions.
- **Authenticated SSR pages now send `Cache-Control: no-store`.** Astro SSR renders user state
  (admin dashboard, tenant settings); bfcache could replay a stale page after the user logs out.
  Response header disables bfcache on authenticated routes. Logout endpoint also sends
  `Clear-Site-Data: *` to purge stored session from browser storage.
- **Role-aware post-login landing via `site/src/lib/auth-landing.ts`.** Helper directs users to
  `/admin/` (admin), `/account/api-keys` (customer), or `/account/repos` (tenant owner) based on
  `is_admin` flag. `is_admin` now returned in login/oauth/verify API responses; middleware role-routing
  edge cases fixed to prevent redirect loops. Tool count stays **24**; no backend/schema change.

### Changed — Auth flow unification (feat/m10b-auth-unify)

- **`/login` is now the canonical login page.** `/admin/login` returns HTTP 301 → `/login`
  (GET-only shim, preserved for backward compat). Astro middleware, nginx, and `/account/*`
  return-redirects all bounce unauthenticated requests to `/login`. OAuth init + callback paths
  `/admin/auth/*` are **unchanged** (no provider-console reconfig).
- **OAuth Google/GitHub buttons added to `/signup`.** Previously only on the login page; a shared
  verb-aware `OAuthButtons` component now surfaces them on both pages. A cookie `oauth_from`
  distinguishes login- vs signup-origin so the callback returns the user to the right place.
- **Shared `AuthLayout`** for login + signup eliminates duplicated structure; "Admin Login" wording
  dropped, standardized to "Sign in". Includes a 22-item UX/a11y pass.

### Security — Reset-password policy + TOCTOU guard (feat/m10b-auth-unify)

- **Password policy enforced on `POST /api/auth/reset-password` (FE + BE).** Min-length
  `auth.password_min_length` (default 12) + common-password blocklist; the `/reset-password` page
  mirrors validation client-side for immediate feedback. Weak passwords return HTTP 400.
- **Reset token no longer burned on a rejected weak password.** `verify_password_reset_token` peeks
  the token without consuming it, and the consume path is wrapped in `SELECT ... FOR UPDATE` to close
  a TOCTOU window — a user can retry the same token with a strong password and succeed.
- Tool count stays **24**. No database migration.

### Fixed — Admin Settings deploy bugs: osm_reader sequence grant + CLI dotenv (fix/admin-settings-grants-dotenv)

- **BUG CLASS A — incomplete osm_reader grant (missing SEQUENCE USAGE).** `osm_reader`
  had `INSERT` on `app_settings` (PR #209) but lacked `USAGE` on its backing BIGSERIAL
  sequence `app_settings_id_seq`. Postgres evaluates the `id` column default
  (`nextval('app_settings_id_seq')`) BEFORE the `ON CONFLICT DO NOTHING` check, so the
  MCP `bootstrap_settings_safe()` catalogue UPSERT failed at startup with
  *"permission denied for sequence app_settings_id_seq"*. Fixed in BOTH
  `migrations/m13_010_app_settings.sql` (inside the existing `pg_roles`-guarded grant
  block) and `ops/rls_create_osm_reader.sql` (SSOT), with the stale comment claiming
  "no sequence is needed" corrected. **Audit:** `app_settings_id_seq` is the ONLY
  sequence `osm_reader` was missing — `app_settings_history` / `ee_modules` / `patterns`
  are SELECT-only (no INSERT -> no sequence USAGE).
- **BUG CLASS B — CLI entry points missing `config.init_dotenv()` (ADR-0031).** Three
  DB/env-reading mains did not bootstrap `.env`, so on a fresh box `PG_DSN` resolved to
  an unconfigured fallback and the process authenticated as the wrong user.
  `ops/backfill_patterns.py::main()` was the PRIMARY offender (caused the live backfill
  auth failure during the Admin Settings deploy); also fixed
  `src/indexer/__main__.py::main()` and `src/indexer/seed_patterns.py::main()`. Each now
  calls `config.init_dotenv()` as the first action of `main()`, mirroring
  `src/db/migrate.py::main()` (ADR-0031: `main()`-only, never at module import).
- **Tests.** `tests/test_migration_m13_010.py` gains `has_sequence_privilege` +
  end-to-end `SET ROLE osm_reader` INSERT assertions; new
  `tests/test_cli_init_dotenv.py` is a deterministic AST regression guard for class B.
- **Migration strategy / prod.** No prod redeploy or re-migrate needed after merge:
  prod was hotfixed live (sequence grant applied), and yoyo keys on the migration-id
  hash so the edited `m13_010` file will not re-run. Web-UI/tool surface unchanged —
  **tool count stays 24**.

### Fixed — MFA step-up freshness: permanent 403 on all fresh-MFA-gated routes (fix/mfa-step-up-freshness)

- **Root cause:** `request.session["mfa_verified_at"]` was READ by both `require_admin_with_fresh_mfa`
  (FastAPI dependency in `src/web_ui/auth.py`) and the inline gate in `tenant_settings.py`, but
  was **never written** anywhere in the application. The DB column `active_sessions.mfa_verified_at`
  (migration `m9_005`) also existed but was never populated. Result: every admin route gated by
  fresh-MFA (admin settings incl. signup toggle, plans, EE-modules, patterns; restore endpoint)
  returned `403 "Fresh MFA required"` permanently.
- **Why unnoticed:** `is_test_bypass_active()` short-circuits the fresh-MFA gate under pytest
  (`WEBUI_AUTH_DISABLED=1 + PYTEST_CURRENT_TEST`), masking the missing write in all test runs.
- **Fix:** `totp_login` now writes `request.session["mfa_verified_at"] = time.time()` **and**
  `UPDATE active_sessions SET mfa_verified_at = NOW()` on successful MFA login. Shared helper
  `_check_mfa_freshness(request)` de-duplicates the gate logic between `require_admin_with_fresh_mfa`
  and `tenant_settings.py`.

### Added — MFA step-up freshness (fix/mfa-step-up-freshness)

- **`POST /api/auth/totp/step-up`** — new endpoint for mid-session MFA re-verification. Requires a
  valid session, re-verifies a TOTP or backup code, rate-limited (same per-user counter as
  `totp_login`), sets `session["mfa_verified_at"]` + `active_sessions.mfa_verified_at` on success.
  Audited via `@audit_action("user.login.mfa")`. Returns `403 {error: "mfa_not_enrolled"}` when no
  TOTP is configured.
- **`auth.mfa_freshness_seconds` setting** — new Tier-1 runtime setting (default 300, min 60,
  max 3600, category auth) in `SETTINGS_CATALOGUE` (ADR-0042). Read via `get_mfa_freshness()`
  helper (mirrors `get_session_ttl()`); fallback constant `MFA_FRESHNESS_SECONDS=300` preserved.
  Tier-1 settings count bumps 15 → **16**.
- **`StepUpMfaModal` + `withStepUp()` frontend** — React island detects `403 "Fresh MFA required"`
  sentinel, prompts admin for TOTP code, POSTs to `step-up`, retries original action once on
  success. All admin action islands that trigger fresh-MFA-gated routes are wrapped via
  `withStepUp`. Web-UI only — **tool count stays 24**.
- **ADR-0043** — concretely specifies the `mfa_verified_at` write contract, step-up endpoint
  contract, runtime-configurable window, audit taxonomy, and frontend UX. Supersedes the
  implied-but-unspecified step-up in ADR-0019 and ADR-0022.

### Fixed — UI contrast / accessibility: light-first theme inversion (fix/ui-contrast-light-first)

- **Root cause (systemic):** `site/src/styles/global.css` set `html { color: #E6F2F4; background: #07131A }`
  as the site-wide default — light text on dark. But ~27 app pages (admin/account/tenant/auth) are
  LIGHT surfaces, and Tailwind Preflight forces `input/select/textarea { color: inherit }`, so every
  form control inherited #E6F2F4 on a white background = **1.14:1 (invisible)**. Native `<select>`
  closed values were invisible until OS hover-highlight. **0/97 inputs** set an explicit text colour.
- **Theme inversion:** `html` now defaults to LIGHT (`color: var(--viindoo-dark)`, `background: #fff`);
  dark is opt-in via `html.theme-dark`, applied by a new `theme` prop on `BaseLayout` (default `light`).
  The 4 marketing pages (`index`, `pricing`, `benchmarks`, `bootstrap`) pass `theme="dark"`. Footer is
  theme-aware.
- **A11y tokens (verified WCAG):** added `--viindoo-primary-text` `#00747F` (5.52:1 on white) for cyan
  used as link/body text on light surfaces; bumped `--viindoo-on-dark-dim` `#5A7782 → #7E9BA6`
  (6.38:1 on `bg-0`). Mirrored in `tailwind.config.mjs`.
- **Surface fixes:** `text-white` on `bg-viindoo-primary` (2.33:1) → `text-viindoo-bg-0` (8.06:1) across
  ~16 buttons/islands; cyan links/badges → `text-viindoo-primary-text` / `text-gray-700`; focus rings
  `ring-viindoo-primary` → `ring-viindoo-primary-deep` (≥3:1 per SC 1.4.11); native inputs given
  explicit `text-gray-900 bg-white`; `RepoTable` "Index All" button white→`text-viindoo-bg-0`
  (3.13/2.33 → 6.0/8.06). `reset-password` violet button normalised to brand.
- **Verified PASS, intentionally unchanged:** `gray-400`-on-dark (6.99:1), `blue-600`/`violet-600`/
  `viindoo-secondary` + white buttons (5.17–6.99:1), `InstallSnippets` tabs (marketing dark-only).
- **Verification:** `pnpm build` green; chrome-devtools render-verify of `/admin/login`, `/signup`
  (typed text dark/visible), `/pricing` (marketing still dark). Web-UI only — **tool count stays 24**,
  no backend/schema change.

### Added — M10B P0-ext: RBAC + Quota + UI (4 use cases, feat/m10b-p0-rbac-quota-ui)

- **Migration m13_009** — seed plan `'unlimited'` (quota=0, rpm=0, is_public=FALSE) + add
  `api_keys.rate_limit_override` + `api_keys.quota_override` columns (nullable INT, CHECK >=0).
  Idempotent (`ON CONFLICT DO NOTHING` + `IF NOT EXISTS` guards). ADR-0041 D1/D4/D5.
- **Middleware** — `_resolve_effective_rpm` / `_resolve_effective_quota` helpers route via plan
  slug (`'unlimited'` SSOT per ADR-0041 D5) + per-key overrides. RPM=0 bypass guard for
  unlimited slug. Override `0` = explicit zero allowed (NOT unlimited). Headers
  `X-Quota-Limit` emits `"unlimited"` sentinel when bypass active.
- **API** — `PATCH /api/admin/api-keys/{key_id}/plan` (body: plan_id + nullable overrides;
  `@audit_action` `api_key.set_plan`; cache invalidate). `PATCH /api/admin/users/{user_id}/plan`
  (cascade to all keys; `user.set_plan_cascade`). `POST /api/api-keys/{key_id}/reactivate`
  (admin unconditional, owner-guarded; `api_key.reactivate`). `GET /api/admin/plans` (full
  catalogue incl. `is_public=FALSE`).
- **UI admin** — `/admin/api-keys`: Plan column with inline dropdown + Overrides modal (React
  island) + Reactivate button on inactive-keys table. `/admin/users`: "Set plan for all keys"
  cascade helper per row. `/admin/tenants`: inline repo + profile assignment widget in detail
  panel.
- **UI account** — `/account/api-keys`: Reactivate button on inactive keys. `/account/usage`:
  upgrade hint copy directing paying users to admin until P1 self-serve ships.
- **Docs** — ADR-0041 (unlimited plan + key overrides); ADR-0039 P0-ext amendment; runbook
  §"Plan changes" (admin upgrade flow + cache invalidation sanity + audit log verification);
  CHANGELOG; TASKS.md.

### Notes — M10B P0-ext

- Tool count stays **24**.
- Migration m13_009 required (`python -m src.db.migrate`).
- M10B P1 (Polar.sh adapter + Entitlement Activation API + subscriptions table) still deferred.
- `_PLAN_CACHE` cross-worker propagation 300s TTL applies after PATCH plan operations — see
  runbook §Plan changes §Cache invalidation sanity for operator guidance.
- W-5 known gap: `GET /api/api-keys` does not yet return `plan_id` + overrides; Plan dropdown
  pre-selection blank on page load. Follow-up tracked in TASKS.md.

### Fixed — M10B P0-ext

- Middleware `X-Quota-Limit` header now emits `"unlimited"` sentinel on both
  the success and monthly-429 paths when `plan_info.slug == "__fallback__"`,
  matching the dual-slug bypass in `_check_monthly_quota` (R-6-A). Previously
  the header emitted `"0"` during a Postgres outage even though the request
  was bypassed (observability/enforcement symmetry gap surfaced in R-8 review).
- Middleware 429 response body now redacts internal sentinel plan slugs
  (e.g. `__fallback__`) - they were enforcement discriminators, never
  intended as user-visible plan labels. RPM-429 path was the live leak
  (reachable during Postgres outage); monthly-429 path is defensive
  (structurally unreachable for fallback per L257 short-circuit, pinned
  by regression test).
- Migration `m13_009` header comment updated: removes the stale "W-2 ships
  the bypass guard / do not assign until W-2 lands" warning (W-2 already
  shipped in this PR) and anchors the sentinel semantics to ADR-0041 D5.

### Changed — Post-PR-#200/#204 cleanup

- Backup format: `pg_dump` now writes `postgres.dump` (`-F custom -Z 6`); restore auto-detects (psql for legacy `.sql`, `pg_restore` for `.dump`). ADR-0018 updated. (TD-1)
- Backup retention: `--keep-bundles N` (default 14), `OSM_BACKUP_KEEP` env override. Prevents `/var/backups` unbounded growth. (new finding)
- Neo4j: `docker-compose` env adds `NEO4J_dbms_security_auth__max__failed__attempts=10` (was default 3). Takes effect after next prod container recreate. (TD-4)
- Test harness: `tests/conftest.py` Priority 2 fallback guard against accidental prod-Neo4j collision. New ADR-0040. (TD-2)
- Version: `0.11.1` → `0.13.1` (sync with CHANGELOG state). (FU-4)

### Added — Onboarding UX + ops

- Onboarding UX: forgot-password e2e (backend + UI + login-page link); landing nav adds `/pricing`; `/login` canonical alias; `/login` renders `?error=` banner (formerly `admin/login` — now 301-redirects to `/login`, see auth-unify entry above). (Wave 1D)
- Docs: 3 new runbooks (`nginx-ratelimit`, `offsite-backup`, `neo4j-container-recreate`); `ops/` promotion of `regrant` + `nginx-patch` + offsite systemd template; deploy-logs archive for 2026-05-28 deploy.

## [0.13.1] — 2026-05-28 — Self-host waitlist + post-v0.13.0 cleanup (PR #204)

### Added

- `migrations/m13_008_waitlist_emails.sql` — `waitlist_emails` table (email UNIQUE, plan TEXT with CHECK enum, source TEXT, created_at TIMESTAMPTZ); index `waitlist_emails_created_at_idx` for admin reporting queries. ADR-0039 P1 precursor.
- `src/web_ui/rate_limit.py` — generic per-IP sliding-window rate limiter (asyncio.Lock; per-IP deques; `_prune_stale` for memory bounds; `TRUSTED_PROXY_CIDRS`-aware `get_client_ip`). Extracted for reuse by public endpoints that have no API key.
- `src/web_ui/routes/waitlist.py` — `POST /api/waitlist` endpoint: rate-limited (5 req/min per IP), duplicate-email ON CONFLICT DO NOTHING, admin email notify via SMTP, `Retry-After` header on 429. Replaces 3 Formspree/Google-Forms placeholders on the pricing page.
- `src/web_ui/email.py` — `send_waitlist_notify_email(submitter_email, plan)` helper (logs in dev mode; SMTP in prod).
- Pricing page (`site/src/pages/pricing.astro`) — self-hosted `/api/waitlist` form replaces 3 Formspree/Google-Forms iframes; handles 200/409/429 client-side with user-visible feedback.
- `tests/test_rate_limit.py` — 13 unit tests for per-IP sliding-window limiter + `TRUSTED_PROXY_CIDRS` XFF guard (T1–T6 base + T7–T9 proxy trust + T10–T13 CIDR edge-cases).
- `tests/test_m13_008_migration.py` — 9 migration tests (table schema, UNIQUE, idempotency, CHECK constraint rejects invalid plan, CHECK constraint accepts valid plans).
- `tests/test_waitlist_api.py` — 21 integration tests for `POST /api/waitlist` (happy path, duplicate, rate limit, Retry-After, admin notify, invalid payload).

### Added — Admin Settings Module (ADR-0042)

- Runtime configuration UI for 15 Tier-1 settings (auth + embedding + indexer + mcp)
  + 4 plan tiers + 16 EE modules + 115 patterns, no redeploy needed.
- `migrations/m13_010_app_settings.sql` — `app_settings` + `app_settings_history` tables
  with 3 partial unique indexes for scope x tenant; ADR-0042 storage layer.
- `migrations/m13_011_ee_modules.sql` — `ee_modules` table backfilled from
  `src/data/ee_modules.py` (16 rows); replaces hardcoded dict with DB-driven guard.
- `migrations/m13_012_patterns.sql` — `patterns` table for 115 curated patterns;
  backfill via `ops/backfill_patterns.py` (replaces `src/data/patterns.json`).
- `src/settings.py` — 3-tier resolver: L1 in-memory LRU (60s TTL, bounded 5000) →
  L2 Postgres → L3 code default from `SETTINGS_CATALOGUE`. Tenant override > system > default.
- `src/settings_registry.py` — `SETTINGS_CATALOGUE` with 15 Tier-1 keys, type/validation/
  restart-class/category metadata.
- `src/web_ui/routes/admin_settings.py`, `admin_plans.py`, `admin_ee_modules.py`,
  `admin_patterns.py` — 26 new HTTP routes under `/api/admin/*`.
- `src/web_ui/routes/tenant_settings.py` — per-tenant `quota.*` override endpoints (Phase 1).
- `site/src/pages/admin/settings/*.astro` + 5 React islands — admin settings UI with
  audit trail, undo last-10, reset-to-default, ≥50% drop warning for quota keys.
- `site/src/pages/tenant/settings/*.astro` + 1 React island — tenant quota self-service UI.
- `ops/backfill_patterns.py` — one-shot script to migrate 115 patterns from JSON → DB.
- Tenant admin self-service for per-tenant `quota.*` override (Phase 1).
- Audit trail + undo last-10 + reset-to-default for every mutation (ADR-0021 cross-link).
- MFA fresh gate (5 min) on destructive ops (ADR-0022).
- Bootstrap hook on process start auto-populates `app_settings` system rows (15 keys);
  `bootstrap_settings_safe()` is try/except non-blocking — falls back to code defaults.

### Fixed — Admin Settings

- `src/web_ui/routes/tenant_settings.py`: ON CONFLICT predicate now matches partial unique
  index (`AND tenant_id IS NOT NULL`); prior predicate silently fell back to full-table
  conflict resolution.
- `src/web_ui/routes/tenant_settings.py` reset: history row now records catalogue default
  value instead of NULL (NOT NULL constraint satisfied).

### Tool count
- Unchanged at 24 MCP tools. Admin Settings is web-UI-only — no new MCP tools added.

### Migration
Run `python -m src.db.migrate && python ops/backfill_patterns.py` after deploy.

### Fixed

- `src/web_ui/rate_limit.py get_client_ip` — now honours `TRUSTED_PROXY_CIDRS` guard (port from `login_attempts.py` pattern). XFF header is only trusted when TCP peer is in the configured trusted-proxy CIDR list. Default (empty list) → XFF never trusted, preventing IP spoof in bare-metal deployments.
- `migrations/m13_008_waitlist_emails.sql plan` column — added `CHECK (plan IS NULL OR plan IN ('free', 'pro', 'team'))` to enforce SQL-comment-documented enum at DB layer.
- `ops/rls_create_osm_reader.sql` — added `GRANT SELECT ON TABLE waitlist_emails TO osm_reader` (defensive: future admin viewer page reads without RLS silent-empty bug).

## [0.13.0] — 2026-05-28 — M10B P0: Quota gating + plan schema + usage dashboard (PR #200)

### Added

- `migrations/m13_006_plans.sql` — `plans` table (4 tiers: free-grandfathered/free/pro/team; `limits` JSONB with `rpm` + `monthly_quota`); `api_keys.plan_id` FK (DB-level DEFAULT `'free'` to prevent NOT NULL constraint violation on new INSERT post-migration); `usage_counter` table (`api_key_id`, `period_yyyymm`, `call_count`). ADR-0039 control-plane DDL. (PR #200)
- `migrations/m13_007_usage_counter_cascade.sql` — ON DELETE CASCADE on `usage_counter.api_key_id` FK; prevents cross-test contamination via SERIAL id reuse. (PR #200)
- Plan-aware MCP middleware (`src/mcp/middleware.py`) — per-plan RPM + monthly quota enforcement; `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` + `X-Quota-Limit` / `X-Quota-Remaining` response headers; 429 differentiation (`rpm_exceeded` vs `monthly_quota_exceeded` reason codes). (PR #200)
- `GET /api/account/usage` endpoint (`src/web_ui/routes/account.py`) — returns current plan info + monthly quota counters for the authenticated user. (PR #200)
- `/account/usage` dashboard page (Astro + React island) — customer-facing live quota view reading `usage_counter` directly. (PR #200)
- `/account/*` gate with `?return=` round-trip redirect through `/admin/login` (CWE-601 path-only allowlist). (PR #200)
- Pricing UI synced to m13_006 seed values (Free 100 calls/30 rpm, Pro 10000/120, Team 100000/300, Grandfathered 1000/60); free-tier stale "5 MCP tool calls / day" claim removed from `site/src/pages/pricing.astro`. (PR #200)
- 5 principle-level operator runbooks under `docs/deploy/runbooks/` (RLS cutover, FERNET provision, post-PR OPS, backup+DR drill, prod smoke 24 tools). (PR #200)

### Fixed

- `ops/rls_create_osm_reader.sql` — portable across DB names via `psql -v db_name=$DB_NAME`; GRANT SELECT ON `plans` + GRANT SELECT, INSERT, UPDATE ON `usage_counter` to `osm_reader` role (required after RLS cutover). (PR #200 Wave 1 + post-review fix)
- `.github/workflows/nightly-smoke.yml` — drops `--local-path` flag (removed in PR #162); closes silent CI failures in #164/#167/#168/#178/#195/#198. (PR #200 Wave 1)
- `docs/deploy/pre-launch-checklist.md` — tool signature drift (`model_inspect`/`module_inspect`/`entity_lookup`); item #15 reference to 6 flat tools already removed in v0.6. (PR #200 Wave 1)
- `pg_pool.checkout()` context-manager migration — 6 sites in `src/mcp/middleware.py` corrected to use `PgPool` public API. (PR #200 post-review fix)
- ON DELETE CASCADE structural hardening (`m13_007`) + `try/finally` cleanup in `test_middleware_quota.py` + extended `_reset_mcp_middleware_state` autouse cache list. (PR #200)

---

## [Merged into v0.13.1] — Data completeness + resource RBAC + observability + backup (feat/osm-data-completeness-rbac)

> **Note:** This block was recorded as `[Unreleased]` before v0.13.1 was cut. All changes
> listed here are included in the `[0.13.1]` release above (shipped untagged — see release
> policy note at the top of this file).

7 tool output gaps (G1-G7) + timeout fix (T1) + resource RBAC hardening (R1/R2/R5) + Era1 comodel fix (C2) + Prometheus histogram (M10C) + Neo4j online backup (#13).
**Tool count stays 24** (no new tool signatures, no new params) — no odoo-mcp-client mirror PR needed.
No new Postgres migration. No reindex auto-triggered; OPS re-index/re-embed actions documented in runbook.

### Added

- **`src/metrics.py`** — Prometheus `embedder_batch_duration_seconds` histogram (M10C WI-D1). Registered at `GET /metrics` on MCP port `:8002` (public, no auth — mirrors `/health`; nginx must IP-restrict it — see deploy guide). Buckets: `(0.1, 0.25, 0.5, 1.0, 1.5, 2.5, 5.0, 10.0, 30.0, 60.0)` s. Per-sub-batch observation inside `Qwen3Embedder.embed()`. Cross-process caveat: only query-embed calls in MCP process are visible (batch indexer runs in a separate OS process). `prometheus_client>=0.20` added to `pyproject.toml`. **Relocated from `src/mcp/metrics.py` → `src/metrics.py`** (shared layer) per the code-review pipeline-import fix below.
- **`tests/test_metrics_endpoint.py`** — 9 unit + endpoint tests for Prometheus histogram.
- **`tests/test_resource_tenant_isolation.py`** — 17 parametrized tests for resource RBAC: model/field/method/module/view handlers return scoped data when tenant context is set; no cross-tenant content leak.
- **`tests/test_neo4j_online_backup_roundtrip.py`** — integration round-trip test (export + restore) using testcontainers Neo4j Community image. Marked `neo4j`.

### Changed

#### Tool output completeness (ADR-0023 hardening — G1-G7)

- **`impact_analysis`** — views/methods/super-methods capped at 20 (`LIST_PREVIEW_MAX_ITEMS`) with `├─`/`└─` tree connectors + `... and N more` disclosure. Dependent-modules capped at 30 (`IMPACT_MODULES_MAX`, new constant) with "run with `profile_name=<p>` to scope" hint. Risk score computed from full count (not capped). (`src/mcp/server.py` G1)
- **`find_examples` / `find_style_override`** — adds ANN disclosure line: "showing N of M semantic candidates — increase `limit`" when `limit < ANN_LIMIT`; "ANN capped at 20 candidates" when `limit >= ANN_LIMIT`. (`src/mcp/server.py` G2)
- **`find_deprecated_usage`** — overflow message shows "showing N of M+ hits" (lower-bound total) + kind-filter hint. No new `start_index` parameter (avoids client mirror; full pagination deferred). (`src/mcp/server.py` G3)
- **`_resolve_method` override chain** — capped at 20 with `├─`/`└─` connectors + `... and N more` disclosure + `entity_lookup(method='…')` escape-hatch hint. (`src/mcp/server.py` G4)
- **`odoo://stylesheet` resource** — truncated at `STYLESHEET_RESOURCE_MAX_BYTES = 131_072` (128 KB); `# [truncated at 128 KB — full file: {N} bytes]` prepended. (`src/mcp/resources.py` G5)
- **`describe_module`** — adds `Next: module_inspect(method='dependencies')` hint when depends list > 20 entries. (`src/mcp/server.py` G6)
- **`suggest_pattern`** — adds `odoo://{version}/pattern/{id}` URI escape-hatch in snippet footer. (`src/mcp/server.py` G7)

#### Timeout fix (T1)

- **`setup_indexes()`** — new `CREATE INDEX IF NOT EXISTS FOR (n:Method) ON (n.model, n.odoo_version)` — resolves partial-scan timeout on `model_inspect`/`module_inspect`/`describe_module` for models with 50+ extending modules (e.g. `sale.order`). OPS: admin must re-run `python -m src.cli index --setup-indexes` on prod to create the index on existing data. (`src/indexer/writer_neo4j.py` T1)

#### Resource RBAC hardening (R1/R2)

- **Resource cache key** — gains `::t{tenant_id}` suffix (Option A): admin key → `::t_admin`, tenant key → `::t{id}`. Prevents cross-tenant cache pollution ahead of private-tenant indexing. Pattern + stylesheet handlers exempt (already globally scoped or use `_scope_pred`). (`src/mcp/resources.py` R1)
- **`resources_index` scope filter** — `_fetch_top_models` and `_fetch_indexed_versions` now use `_scope_pred` — discovery URIs are tenant-scoped; avoids over-inclusive `resources/list` response. (`src/mcp/resources_index.py` R2)
- **Cross-process scope cache invalidation** — DEFERRED (R3): staleness bounded at 60s TTL; Redis/PG-NOTIFY deferred to M14+.

#### Era1 comodel fix (C2)

- **`parser_python.py` `_extract_columns_dict_fields()`** — now extracts `comodel_name` for Many2one/One2many/Many2many from AST-parseable v8/v9 files (positional arg or `comodel_name` kwarg). Previously only the text-regex fallback path did this. Fixes `resolve_orm_chain` on v8/v9 AST-path modules. 2 regression tests added. OPS: re-index v8/v9 `--full` required. (`src/indexer/parser_python.py` C2)

#### Neo4j online backup (ADR-0018 update — WI-D2)

- **`src/cli.py`** — `backup` command now exports Neo4j via Bolt driver streaming (`MATCH (n) RETURN …` → CREATE + MATCH/MERGE relationship statements). Bundle contains `neo4j.cypher` (text, online) instead of `neo4j.dump` (binary, offline). Neo4j stays running during backup. Zero new server-side deps (uses existing `neo4j` Python package; no APOC, no Enterprise). `restore` auto-detects `neo4j.cypher` vs legacy `neo4j.dump` (prints manual-restore note for old bundles). **A Neo4j restore failure now propagates a non-zero exit code** (see code-review fixes below — superseded the original non-fatal behaviour).
- **`docs/adr/0018-backup-contract.md`** — updated contract (neo4j.dump → neo4j.cypher), rationale, restore prerequisites, consequences. (`src/cli.py`, `docs/adr/0018-backup-contract.md`)

### Fixed (code review — PR #189)

- **DR safety in `restore` (`src/cli.py`, ADR-0018)** — three hardening fixes so a corrupt/partial Neo4j restore can no longer silently destroy the live graph:
  - `_restore_neo4j_cypher` now **validates the cypher file before the destructive `MATCH (n) DETACH DELETE n`**: ≥1 executable statement AND the export completeness trailer (`REMOVE n.__eid__`) must be present. An empty/truncated dump returns an error and the graph is never wiped.
  - `_restore_bundle` writes a **pre-restore Neo4j safety snapshot** (`pre-restore-<ts>-neo4j.cypher` via `_export_neo4j_online`) into `BACKUP_DIR` — parity with the existing Postgres safety backup. If the live graph is reachable but the snapshot fails, the restore aborts before wiping; if Neo4j is unreachable/unconfigured (so the restore cannot wipe anything either), the snapshot is skipped.
  - A **Neo4j restore failure now propagates a non-zero exit code** (Postgres success is still reported). Previously a failed/partial graph exited `0`, hiding the failure from DR automation.
- **Tree connector (`src/mcp/server.py`, ADR-0023 §1.2)** — the `_resolve_method` override-chain and `impact_analysis` (`_append_capped_section`) renderers now delegate connector assignment to `render_list_block`, so the `... and N more` disclosure row gets the `└─` connector as the last child (it was previously emitted without any connector).
- **Memory + snapshot consistency in `_export_neo4j_online` (`src/cli.py`)** — the export now **streams each statement straight to the file handle** instead of accumulating the whole graph in an in-memory list (ADR-0018 sizes the graph at ~1-2M nodes), and reads nodes + relationships inside **one explicit read transaction** so a concurrent indexer write cannot produce a dangling-relationship dump. Output format is byte-identical (round-trip test unchanged).
- **Pipeline import discipline** — `embedder_batch_duration_seconds` metric **relocated `src/mcp/metrics.py` → `src/metrics.py`** so `src/indexer/embedder.py` no longer imports the server (`src.mcp`) layer (one-way pipeline rule, CLAUDE.md). New `tests/test_pipeline_import_discipline.py` guards the rule via static AST analysis.
- **Deterministic ORDER BY in `resources_index._fetch_top_models` (`src/mcp/resources_index.py`)** — added `mod.name ASC` tiebreak so the discovery index order is stable when one model name is defined by several modules at the same `dep_count` (Neo4j 5.x gotcha).
- **Observability invariant doc (`src/indexer/embedder.py`, ADR-0010 D7)** — corrected the comment + ADR that claimed `_hist.observe` and `call_count += 1` are co-located in the same critical section. They are co-located only on the single-batch path; on the multi-batch path `observe()` runs per sub-batch (correct latency granularity) and `call_count += 1` once per `embed()` call. No metric-semantics change.
- **`/metrics` nginx hardening (docs-only)** — `docs/deploy.md` + `docs/deploy/nginx-m8.conf` now document and template an IP-restricted `location = /metrics` (allow scraper IP / `deny all`), since `/metrics` bypasses app-layer auth (standard Prometheus pattern; mitigation at the proxy).
- **Tests** — `tests/test_cli_restore_bundle.py` extended (empty/truncated-dump refuse-to-wipe; safety-snapshot-failure aborts; Neo4j-failure non-zero exit; single-transaction + streaming export contract); `tests/test_tree_disclosure_connector.py` (disclosure-row `└─` contract); `tests/test_pipeline_import_discipline.py` (indexer ✗→ mcp).

### OPS — admin actions required on production (code done, not yet run)

See `docs/deploy/reindex-v8-v19-runbook.md §Post-PR Wave (feat/osm-data-completeness-rbac)` for the full checklist. Summary:

1. **Re-run `setup_indexes()`** — creates `Method(model, odoo_version)` index (T1 timeout fix).
2. **Re-index v8/v9 `--full`** — materializes `comodel_name` on Field nodes (Era1 C2 fix).
3. **Re-embed v9.0** — `find_examples` v9 returns empty; suspected partial re-embed on prod.
4. **M13 close OPS (pre-existing):** `ops/cleanup_absolute_path_nodes.cypher`, RLS FORCE cutover (`osm_reader` role + DSN split), FERNET credstore cut — see runbook §5.14.

---

## [Merged into v0.13.1] — Web-UI multi-tenant RBAC + self-service portal (W0-W4)

> **Note:** This block was recorded as `[Unreleased]` before v0.13.1 was cut. All changes
> listed here are included in the `[0.13.1]` release above (shipped untagged — see release
> policy note at the top of this file).

Batch 5 PRs (#174/#177/#179/#180/#181). **DOCS-ONLY wave này (W5).** Tool count stays **24**. Một Postgres migration mới (`m13_005_tenant_members.sql`) — admin phải chạy `python -m src.db.migrate` trước khi deploy. Không cần reindex.

### Fixed — sync-tool context propagation: ContextVar replaces threading.local() (fix/sync-tool-context-propagation, #197)

- **Bug:** `set_active_version` / `set_active_profile` crashed on the live server with
  `invalid literal for int() with base 10: 'default'`. Root cause is a **coroutine race**, not a
  worker-thread issue: asyncio multiplexes all concurrent requests on a single event-loop thread, so
  the `threading.local()` (`_api_key_id_local` / `_tenant_id_local`) populated by `UsageLogMiddleware`
  was **shared** across coroutines — one request's `finally` (`del .value`) wiped the value mid-flight
  of another, so the sync tool body read the `'default'` sentinel → `int('default')`. FastMCP 2.14.7
  runs sync `@mcp.tool` bodies inline on the event-loop thread (no `anyio.to_thread`), so a single
  sequential request never crashed — only concurrent prod traffic + fire-and-forget log/audit tasks
  triggered it.
- **Fix:** `src/mcp/server.py` — `_api_key_id_var` / `_tenant_id_var` are now `contextvars.ContextVar`
  (each coroutine gets its own copy; also propagates into `anyio.to_thread` if FastMCP ever offloads
  sync tools). `src/mcp/tool_log_middleware.py` — `_set_server_*` return tokens; `on_call_tool` /
  `on_read_resource` use token-reset in `finally`. `src/mcp/session.py` — belt-and-suspenders: a
  non-numeric `api_key_id` (the `'default'` sentinel / stdio transport) now skips the DB op instead of
  raising.
- **Blast radius:** `_tenant_id_var` had the same race; it feeds RLS/tenant scoping. The fix closes a
  latent fail-OPEN window (a tenant request whose `tenant_id` got wiped → `None` → unrestricted). With
  the ContextVar fix each coroutine's tenant_id is isolated. `tenant_id=None` remains "global/admin key
  by design", never "lost → leak". Pre-existing (session/middleware last touched by #171/#162/#155, not
  the #191–196 cleanup wave).
- **Tests:** `tests/test_context_propagation.py` — ContextVar isolation under `asyncio.gather`; a
  deterministic (`asyncio.Event`) reproduction of the historical `threading.local` wipe; an
  **end-to-end** test driving a real sync `@mcp.tool` through the real `UsageLogMiddleware` via an
  in-memory `fastmcp.Client` (asserts the tool body sees the authenticated context, not `'default'`);
  session non-numeric guards. 5 existing test files migrated to the `_var.set()/.reset(token)` API.

### Fixed — M13 heal stale unresolved flags on already-resolved nodes/edges (fix/m13-heal-resolved-unresolved-flags)

- **`ops/cleanup_resolved_unresolved_flags.cypher`** (new) — one-time prod heal for the "Residual 2"
  scenario: 153 View/QWebTmpl nodes (`module<>'__unresolved__'`, `unresolved=true`) and 326 incident
  edges (`unresolved=true`) that survived the previous `ops/cleanup_unresolved_placeholders.cypher`
  because those nodes had already had their `module` rewritten to a real value by an old write pass,
  so the placeholder-deletion script (which targets `module='__unresolved__'`) left them intact.
  The heal script only SETs `unresolved=false`; it does NOT delete any nodes or edges.  Idempotent.
  Run: `docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" -f /dev/stdin < ops/cleanup_resolved_unresolved_flags.cypher`
  Expected: `nodes_healed ≈ 153`, `edges_healed ≈ 326`; zero on rerun.
- **`src/indexer/writer_neo4j.py::Neo4jWriter.heal_resolved_unresolved_flags`** (new method) —
  defense-in-depth heal called automatically at the end of `gc_unresolved_placeholders`.  Clears
  `unresolved=true` on any `View`/`QWebTmpl` node whose `module <> '__unresolved__'` (real by
  definition) and on any edge whose target is a real node, scoped by `odoo_version`.  Future
  stragglers heal automatically at the next `--gc` run without operator action.
- **`docs/adr/0007-incremental-indexer.md` §D5** — documented Residual-2 scenario, correctness
  argument, and new method in implementation references.
- **`tests/test_gc_unresolved_placeholders.py`** (6 new tests in `TestHealResolvedUnresolvedFlags`)
  — View node+edge healed; QWebTmpl node+edge healed; genuine placeholder preserved (not
  false-healed into a phantom real node); version scoping; idempotent; gc wires heal automatically.

### Fixed — M13 index hygiene (feat/m13-cleanup-automation, #194)

- **`ops/cleanup_test_sentinel_modules.cypher`** (new) — removes 2 test-sentinel Module nodes
  (`lt_globex_only` v97.0 + `lt_globex_only2` v96.0) that leaked into prod Neo4j from a test
  run against the live DB.  Nodes have `path=NULL`, `repo_id=NULL`, 0 edges; inert but pollute
  raw Neo4j version queries.  Scoped by exact `(name, odoo_version)` pair; idempotent.
- **`src/indexer/incremental.py` docstring fix** — `filter_modules_by_changed` docstring
  incorrectly claimed `ModuleInfo.path` is "typically relative".  Corrected: `ModuleInfo.path`
  holds the ABSOLUTE module directory (`str(module_dir)`, `registry.py:266`); `pipeline.py:312`
  converts `git diff` relative paths to absolute before passing them to `filter_modules_by_changed`
  so the equality is absolute-vs-absolute and correct.  No logic change.
- **`src/indexer/writer_neo4j.py` — View / QWebTmpl placeholder MERGE key fix** — placeholder
  nodes for unresolved `INHERITS_VIEW` / `EXTENDS_TMPL` targets previously used a 3-property
  MERGE key `{xmlid, module:'__unresolved__', odoo_version}` while the real node uses 2-property
  `{xmlid, odoo_version}`.  Key divergence produced 54 "shadow" View pairs on prod (one real +
  one placeholder for the same `xmlid+version`).  Fix: placeholder MERGE now uses the same 2-key
  so it converges on the real node when it already exists; `ON CREATE` stamps `unresolved=true` +
  `module='__unresolved__'` only for genuinely new placeholders.  No schema migration; no reindex.
- **`src/indexer/writer_neo4j.py` — View / QWebTmpl `unresolved` flag cleared on real write**
  (residual gap from MERGE-key fix above) — after key convergence, a real View/QWebTmpl write
  lands on the same node as the placeholder (no shadow), but the real SET block did not clear
  `unresolved=true`.  The converged node ended up `module=<real>, unresolved=true`, causing
  node-level filters in `server.py` (~l.986, ~l.977, ~l.722, ~l.1421, ~l.3986) to wrongly hide
  the view even though its module was already resolved.  Fix: real View and QWebTmpl SET blocks
  now unconditionally write `v.unresolved = false` / `t.unresolved = false` (a node appearing in
  `result.views`/`result.qweb` IS real/resolved by definition).  Model and OWLComp are NOT
  affected: their MERGE key includes `module` (`{name, module, odoo_version}`), so a real write
  never lands on a placeholder — their placeholder (`module='__unresolved__'`) and real
  (`module=<real>`) are always distinct nodes.  Edge-staleness one-liner also applied: resolved
  `INHERITS_VIEW` and `EXTENDS_TMPL` MERGEs now include `ON MATCH SET r.unresolved = false` so
  an old `{unresolved:true}` edge from the placeholder phase is cleared the next time the child
  is re-indexed (rather than waiting for a `--gc` run).
- **`src/indexer/writer_neo4j.py::gc_unresolved_placeholders`** (new method) — DETACH DELETEs
  all `{unresolved:true, module:'__unresolved__'}` placeholder nodes scoped by `odoo_version`.
  MCP server already filters these at read time (30+ `module <> '__unresolved__'` sites); safe to
  remove.  Called automatically when `--gc` is requested (alongside existing `gc_stale_modules`).
- **`ops/cleanup_unresolved_placeholders.cypher`** (new) — one-time ops script for existing prod
  graph (2,068 placeholder nodes / ~5,404 `{unresolved:true}` edges / 54 View shadow pairs).
  Run before or after deploying this PR; `--gc` handles future accumulation.
- **`docs/adr/0007-incremental-indexer.md` §D5** — updated to document M13 placeholder GC
  extension and View MERGE-key fix.
- **`tests/test_gc_unresolved_placeholders.py`** (9 tests, 2 new for the `unresolved` flag gap) —
  regression: no shadow View after writer fix; `unresolved` flag cleared after real write for both
  View and QWebTmpl; gc removes placeholders; gc preserves real nodes; gc is idempotent; gc is
  version-scoped.  All pass.

### Added — WI-7 FERNET credstore cut (feat/wi7-fernet-credstore-cut)

- **[WI-7] FERNET key delivered via systemd credential store (webui+backup `LoadCredential`,
  CLI via `osm-fernet-run`); removed from `.env`/`webui.env`. RLS enforcement still pending.**
  - `docs/deploy/odoo-semantic-webui.service` — `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY`
    now active (replaces the commented-out line from #185). Key lives root:root 0600 at
    `/etc/credstore/FERNET_KEY`; PREREQUISITE: provision before enabling the unit
    (missing source = 243/CREDENTIALS hard-fail, NOT a soft fallback).
  - `docs/deploy/odoo-semantic-backup.service` — same `LoadCredential=` added so the opt-in
    `--bundle-passphrase-env` DR bundle (`fernet.enc`, ADR-0018) sources `FERNET_KEY` from
    credstore. The nightly bundle (`postgres.sql` + `neo4j.dump` + `manifest.json`) contains
    no `fernet.enc` and does not read FERNET (the credstore source must still exist, else the
    unit hard-fails 243/CREDENTIALS at startup).
  - `docs/deploy/osm-fernet-run` (new, mode 0755) — `systemd-run -p LoadCredential=` wrapper
    for ad-hoc CLI (indexer/rotate-fernet/restore); closes the CLI delivery gap; must run as root.
  - `docs/adr/0020-fernet-key-delivery.md` — §5 and §6 updated: holistic cut realized;
    "zero net hardening / commented out" caveat resolved; 243/CREDENTIALS hard-fail warning retained;
    `$FERNET_KEY` env fallback for dev/non-systemd preserved.
  - `docs/deploy.md §12` Option B — updated to final design: provision credstore with EXISTING
    key, strict ordering, CLI via wrapper, 24.04+26.04 compatibility.
  - `docs/deploy/install-runbook.md` — REQUIRED credstore-provision step added before
    `systemctl enable --now` of webui/backup units.
  - `docs/deploy/reindex-v8-v19-runbook.md §FERNET cutover` — updated from "commented out /
    provision before uncommenting" to "LoadCredential now active; provision credstore as prerequisite".
  - `docs/deploy/backup-runbook.md` — FERNET delivery section added; ad-hoc CLI via `osm-fernet-run`.
  - `TASKS.md WI-7` — FERNET credstore sub-items marked `[x]` DONE; RLS sub-items remain `[ ]` pending.
  - Prod unaffected until the /tmp ops scripts (credstore provision + restart sequence) run.
  - RLS enforcement (`osm_reader`, `FORCE ROW LEVEL SECURITY`, DSN switch) explicitly OUT of
    scope for this PR — separate effort requiring prior code changes.

### Fixed — webui unit LoadCredential decoupled (#185)

- **`docs/deploy/odoo-semantic-webui.service`** — commented out
  `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY` (was added in #173, caused
  status=243/CREDENTIALS on prod where `/etc/credstore/FERNET_KEY` does not yet exist).
  Root cause: systemd `LoadCredential` with a missing source is a **hard fail**, not a
  soft fallback to `EnvironmentFile=`. Additionally, `src/cli.py` (indexer +
  `rotate-fernet`) reads FERNET_KEY from env/`.env` only (no credential access), so
  a webui-only LoadCredential provides zero net hardening while risking a boot failure.
  The holistic WI-7 OPS cut (credstore + CLI coverage + `.env` removal) is the correct
  path; env delivery is the uniform source until then. No code change; unit template + docs only.

### W0 (#174) — Admin gate + SIGNUP_ENABLED

#### Added
- **`SIGNUP_ENABLED` config flag** (`src/web_ui/config.py`) — default `False` (invite-only). Đọc từ env var `SIGNUP_ENABLED=1` hoặc INI `[webui] signup_enabled = true`. Khi `False`, `POST /api/auth/register` và OAuth new-account path trả 403. Xem `docs/deploy.md §Auth - SIGNUP_ENABLED`.
- **`Depends(require_admin)` áp lên 19 route mutating** — repos, ssh_keys, operations, jobs. Route `restore` giữ `require_admin_with_fresh_mfa`. Self-service routes (api_keys/totp/feedback) giữ ownership-scope.

### W1 (#177) — Tenant membership + admin tenant CRUD (ADR-0038)

#### Added (migration required)
- **`migrations/m13_005_tenant_members.sql`** — 3-part migration:
  - `tenant_members(user_id, tenant_id, role, created_at)` M:N join table; `PRIMARY KEY (user_id, tenant_id)`.
  - `ALTER TABLE webui_users ALTER COLUMN password_hash DROP NOT NULL` — đóng issue #176 (OAuth-only users đã INSERT NULL trên prod).
  - `CHECK (profiles.name NOT LIKE '%,%')` — GUC-delimiter guard ngăn profile name chứa dấu phẩy, bảo vệ RLS `string_to_array` (ADR-0034 A4).
- **`resolve_tenant_scope_web(request)` / `ALL_TENANTS` / `is_in_scope`** trong `src/web_ui/auth.py` — write-side scope helper (admin = `ALL_TENANTS` sentinel; non-admin = set of tenant_id from `tenant_members`).
- **`routes/tenants.py`** — admin-only tenant/member/resource CRUD: `GET/POST /api/tenants`, `DELETE /api/tenants/{id}` (409 nếu còn resources), `GET/POST/DELETE /api/tenants/{id}/members`.
- **Astro page `/admin/tenants`** — quản lý tenant + thành viên (admin-only).
- **Membership model (b)** — user đa-tenant (consultant/agency persona). Active-tenant = **Option A** (explicit `tenant_id` trong request body, stateless, auditable).

#### Notes
- `#175` (audit coverage) đã FOLD vào W3; `#176` (password_hash nullable) đã FOLD vào W1 m13_005. Cả hai CLOSED.
- ADR-0038 `docs/adr/0038-tenant-rbac-web-ui-write-side.md` committed.

### W2 (#179) — Customer self-service portal

#### Added
- **`tenant_write_allowed(scope, tenant_id)`** trong `src/web_ui/auth.py` — write-side guard STRICTER than `is_in_scope`: `tenant_id IS NULL` (shared) → admin-only write; non-admin chỉ write vào tenant của mình.
- **`GET /api/repos/profiles` tenant-filtered** — non-admin chỉ thấy profile trong scope (`is_in_scope`) + shared; `tenant_id` field có trong mỗi profile/repo response.
- **4 route repo mở cho non-admin với tenant scope:**
  - `POST /api/repos/repos` — thêm repo vào tenant-owned profile
  - `PATCH /api/repos/repos/{id}` — cập nhật repo metadata trong scope
  - `DELETE /api/repos/repos/{id}` — xóa repo trong scope
  - `POST /api/repos/repos/{id}/index` — trigger index cho repo trong scope
- **`GET /api/account/tenants`** (`routes/account.py`) — trả danh sách tenant của session user kèm `role` (portal header).
- **Astro page `/account/repos`** — customer self-service repo management.

#### Notes (ADR-0038 D9-D13)
- Admin-only routes (profile CRUD, bulk ops, tenant CRUD, SSH keys, operations) KHÔNG thay đổi từ W0/W1.
- **SSH key cho non-admin (ADR-0038 D13):** non-admin quản lý repo SSH KHÔNG chọn key — server resolve key access dùng chung (`key_type='access_key'`, lấy row đầu theo id); client-supplied `ssh_key_id` của non-admin bị bỏ qua. Áp dụng cho **cả `POST add_repo` lẫn `PATCH update_repo`**: trên PATCH, `ssh_key_id`/`clear_ssh_key` của non-admin bị bỏ qua (giữ nguyên key hiện có; chỉ resolve shared key khi URL chuyển sang SSH mà repo chưa có key) — đóng lỗ chọn key chéo-tenant trên đường PATCH (code review PR #183). Admin vẫn chọn key từ dropdown trên cả hai route. Portal `/account/repos` hiển thị hướng dẫn: user tự thêm public key (admin công bố) vào git host của mình.

### W3 (#180) — Diagnostics + admin user creation + audit coverage

#### Added
- **`GET /api/operations/diagnose`** — delegate sang `src/diagnostics.py` (SSOT dùng chung với CLI `diagnose` subcommand). Trả trạng thái Postgres, Neo4j, Ollama, FERNET_KEY, config.
- **`src/diagnostics.py`** — module SSOT, tách khỏi `cli.py`.
- **`POST /api/admin/users`** (`routes/admin_users.py`) — admin tạo user mới với temp-pass hoặc invite link (one-time).
- **`GET /api/admin/audit-log`** — paginated + filterable audit log viewer (admin-only).
- **Trang `/admin/audit-log`** (Astro SSR).
- **`@audit_action` mở rộng** — bổ sung cho: `operations.index_all`, `jobs.reset`, `user.deactivate`, `user.reactivate`, `user.reset_password_link` (5 action mới).
- **Regression guard `enumerate-app`** — test kiểm tra mọi route mutating (HTTP method != GET) gắn với admin phải có `__audit_action__` marker; fail khi thêm route mới mà quên audit.

#### Changed
- ADR-0021 taxonomy cập nhật với 5 action mới.
- **BREAKING (CLI `osm diagnose --json`):** schema thống nhất theo SSOT `src/diagnostics.py` — mỗi check đổi key `"check"` → `"name"` và trạng thái lỗi `"status": "fail"` → `"status": "error"` (giá trị hợp lệ nay là `ok`/`error`/`skipped`), kèm trường `"overall": "ok"|"degraded"`. HTTP `GET /api/operations/diagnose` dùng cùng schema. Pipeline cron/alert nào parse output `--json` cũ (`check`/`fail`) cần cập nhật key.

### W4 (#181) — Data-driven version list + worker controls

#### Added
- **`GET /api/versions`** (`routes/versions.py`) — đọc `src/indexer/spec_data/bootstrap_versions.json` (12 phiên bản v8-v19), sort numeric, trả `{"versions": ["8.0", ..., "19.0"]}`. Dùng cho các dropdown version trong Admin UI.
- **3 dropdown version trong Admin UI** — index-core, seed-patterns (thêm option 'all'), add-repo (populate từ `GET /api/versions`).
- **Worker controls trong index-all:** `profile_workers` (1-4, parallel profiles) + `max_workers` (1-8, parallel repos per profile) + `--gc` flag (cleanup stale Module nodes).
- **Branch hint** trong form add-repo — chọn version ở dropdown tự pre-fill ô branch input (ví dụ chọn `17.0` → branch input điền sẵn `17.0`); user vẫn sửa được.

---

## [Merged into v0.11.0] — WI-7 FERNET hardening + RLS armed-but-dormant + Path portability (ADR-0037)

> **Note:** This block was recorded as `[Unreleased]` before v0.11.0 was cut. All changes
> listed here are included in the `[0.11.0]` release below (shipped untagged — see release
> policy note at the top of this file).

### WI-7 — FERNET secrets hardening (M13)

**Security / breaking change.** No reindex required.

#### Changed
- **Central FERNET key getter (`src/crypto.py`)** — new `get_fernet_key()` /
  `get_fernet()` with two-source resolution: `$CREDENTIALS_DIRECTORY/FERNET_KEY`
  (systemd `LoadCredential`, preferred) → `$FERNET_KEY` env var (backward-compatible
  fallback). All five call sites refactored to use the central getter.
- **`rotate-fernet` now covers `totp_secrets`** — `totp_secrets.secret_encrypted`
  is re-encrypted in the same atomic transaction as `ssh_key_pairs.private_key_encrypted`.
  `row_count` in `key_rotation_log` = ssh_rows + totp_rows. If any row in either
  table fails to decrypt → rollback all.

#### Removed (breaking)
- **`--old-key` / `--new-key` CLI flags** removed from `rotate-fernet` sub-command.
  These flags were deprecated in M9 (ADR-0020 F13) and promised removal in M10.
  **Migration:** use `--old-key-env OLD_FERNET_KEY --new-key-env NEW_FERNET_KEY`
  (already the default) or set env vars directly.

#### Docs
- ADR-0020 updated: WI-7 findings, central getter, LoadCredential delivery,
  extended rotation atomicity, Consequences section.
- `docs/deploy.md` §12: LoadCredential OPS cutover steps + rotation flow update.

### WI-7 — RLS policy armed-but-dormant (M13, migration m13_004)

**Security / defense-in-depth.** No reindex required. Tool count stays **24**.

#### Added
- **`migrations/m13_004_embeddings_rls.sql`** — `ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY`
  + `CREATE POLICY embeddings_tenant` dùng GUC `app.allowed_profiles` (sentinels: `'*'` = admin,
  `IS NULL` = shared, `= ANY(string_to_array(...))` = tenant). Policy wired vào read path MCP tier
  qua `SET LOCAL app.allowed_profiles` per request (code trong `src/mcp/server.py`).
- **`docs/deploy/odoo-semantic-webui.service`** — `LoadCredential=FERNET_KEY:/etc/credstore/FERNET_KEY`
  initially added (#173), then **commented out** (#185): a missing `/etc/credstore/FERNET_KEY`
  hard-fails the unit at status=243/CREDENTIALS (NOT a soft fallback); `src/cli.py` (indexer +
  `rotate-fernet`) also needs FERNET_KEY via env and has no systemd credential access — env
  delivery is the uniform source for all consumers until WI-7 holistic OPS cut. The shipped
  template uses `EnvironmentFile=` only; LoadCredential will be uncommented at cut time.

#### Behaviour note
Migration này là **no-op trên production cho đến khi OPS chạy runbook §5.14**: app connect
bằng owner role (`odoo_semantic`), `ENABLE` không `FORCE` = owner bypass = policy không có
hiệu lực. Read-guard thực sự vẫn là SQL `AND profile_name = ANY(%s)` (WI-4, shipped v0.10.0).
`FORCE ROW LEVEL SECURITY` + non-owner read role `osm_reader` + tách read-DSN của MCP tier
là các bước OPS thủ công (reindex runbook §5.14), KHÔNG chạy tự động.

#### Docs
- ADR-0034 Amendment A4: giải thích partial landing, known-constraint GUC delimiter,
  quan hệ với A2.
- Reindex runbook §5.14: hướng dẫn FORCE + role + DSN-split + verify + rollback.
- `m13_001` comment cập nhật: trỏ đúng sang m13_004 thay vì "deferred to a later migration".

---

Single PR. File paths are now **repo-relative everywhere** instead of server-absolute,
so an AI client on a different machine can map them onto its own checkout, and moving
the server to a new host no longer requires a reindex. Tool surface stays **24**.
Requires a full reindex v8→v19 after deploy + post-reindex cleanup (see below).

### Changed
- **Stored paths are repo-relative** (`addons/sale/models/sale_order.py`), not absolute.
  `repos.local_path` is the single absolute anchor. Relativization happens at the writer
  boundary via a transient `ModuleInfo.repo_root` (set in `build_registry`): `Module.path`,
  `OWLComp/JSPatch.file_path`, `Stylesheet.file_path` + `@import` targets (writer_neo4j),
  and `embeddings.file_path` for method/field/view/qweb/js + css/scss/less (writer_pgvector).
- **CoreSymbol / CLICommand** relativize against the Odoo source root in their parser
  (`odoo/orm/models.py`, `odoo/cli/server.py`) — they have no `repos` anchor.
- **8 MCP render sites** emit repo-relative paths via the new `_portable_path()` helper
  (find_examples, lookup_core_api, describe_module, module_inspect JS, resolve_stylesheet,
  find_style_override, + import/override chains). Idempotent → permanent safety-net for
  any legacy absolute row even before the reindex lands.
- **Repo identity is the portable git URL, not the server dirname.** Every `[repo]` label
  and the `describe_module` repo line now show `repo_url` (e.g. `github.com/odoo/odoo`)
  instead of the host checkout dirname (`odoo_17.0`) — the dirname is server-detail an AI
  client can't use. Neo4j-sourced tools coalesce `repo_url`→`repo` in-query (zero render
  edits); `find_examples` resolves `repo_id`→url at render (cached); dirname remains a
  fallback only when no URL is known. (PR review — AI-client lens.)
- **Server migration is now a `local_path` re-point, no reindex**: the `odoo://stylesheet`
  resource reconstructs the absolute on-disk path dynamically from `repos.local_path`
  (`resources.py`), and the DR runbook documents the re-point + cache-clear procedure.

### Fixed
- **Provenance gap**: css/scss/less embedding chunks now carry `repo` + `repo_id`
  (previously only `module` + `odoo_version`), so dropping the absolute path loses no
  identifying information.
- **GC alignment**: `live_paths` is relativized to match the relative `Module.path` —
  prevents the catastrophic case where every module looks stale and gets deleted.

### Ops
- Full reindex v8→v19 required. **After** it completes, run
  `ops/cleanup_absolute_path_nodes.cypher` to drop stale absolute-keyed Stylesheet /
  LintViolation nodes (their `file_path` is a MERGE-key component). Verify Neo4j +
  `embeddings WHERE file_path LIKE '/%'` are 0. See reindex runbook §3b.

### Docs
- ADR-0037 (path portability); reindex runbook §3b; disaster-recovery §Migration to New Host.

## [0.11.1] — 2026-05-23 — Pre-LIVE hygiene (read-side; no reindex)

Small follow-up after #165 (v0.11.0). **Read-side only** — no parser/writer change, no
new migration, **no reindex required**. Tool surface stays **24**.

### Removed
- **`scripts/cleanup_v96.cypher` + `tests/test_no_v96_data.py`** (stale one-shot relics).
  The script was an unguarded, label-blind `DETACH DELETE n WHERE n.odoo_version='96.0'`
  with zero operational wiring; `96.0` is now an active test-sentinel version (the 94-99
  band, alongside `TEST_VERSION=99.0`), so the guard test was a false-positive generator
  (it asserted 0 nodes at `96.0` on a DB where sibling tests legitimately seed `96.0`).
  The runbook §1a `snap_mod`-scoped cleanup (name+version pinned) supersedes the script,
  and the mandatory full reindex v8→v19 rebuilds the graph regardless.

### Documented (no behaviour change)
- **R-1 — `describe_module` depends-list intentionally unscoped** (ADR-0034 T7): code
  comments at `_describe_module` + `_describe_module_structured` explain why the manifest
  depends list returns names with no `_scope_pred("d")` — the asymmetry with the
  content-returning `_module_dep_closure` is by design (the list returns only names from
  the caller's own scoped manifest; the closure returns `dep.repo`/`repo_url` and so must
  filter). Confirmed not-a-leak; documented to prevent re-flagging.
- **Public-share semantics + future direction** (ADR-0034 T6): the binary
  `tenant_id IS NULL` = shared model is the launch design; re-classification is a
  read-side `tenant_id` flip (no reindex); per-repo / per-tenant publishing is a deferred
  product feature, **not** a gate for going multi-tenant LIVE. Runbook §5.12c cross-refs.
- **MED-3 — cross-tenant over-eager re-index** (reindex runbook Known Constraints):
  `find_dependent_repos` + basename-collision can NULL another tenant's `head_sha`
  (integrity/cost, **not** a confidentiality leak); accepted at current scale, revisit
  before scaling tenant count (ADR-0007 W14, ADR-0034 A3).

## [0.11.0] — 2026-05-23 — Parser correctness v8-v19, arch_snippet, tenant isolation, query/render, enrichment (WG-1..WG-5)

Six work-groups landed on `feat/osm-final-stretch` via the fix-wave integration branch.
Tool surface stays **24**. No new Postgres migrations. Requires a full reindex v8→v19
after deploy (see runbook §5.11-5.12 for the new pre-traffic multi-tenant gate).

### Added / Fixed — WG-1: Python parser correctness (v8-v19)
- **v9 Py2-syntax fallback**: `ast.parse` failure on Python-2-only tokens (`<>`, etc.)
  now falls back to `_parse_era1_text()` regex for both `_columns` AND `fields.X` new-API
  fields — prevents `account.py` losing 82 fields on v9 reindex.
- **`Many2oneReference` + `PropertiesDefinition` + `property` field types**: added to
  `FIELD_TYPES` (v13+ `Many2oneReference`; v16+ `PropertiesDefinition`;
  v8/v9 legacy `fields.property`). Previously caused silent Field node drops.
- **F-14 Selection positional guard**: `fields.Selection('_compute_sel')` positional
  string no longer stored as `string=` label.

### Added / Fixed — WG-2: JS parser + query.py path + NewId (v8-v19)
- **OWLComp dual-dispatch (JS-G1)**: `parser_js.py` era2 files for major>=14 now also
  call `_extract_era3_components()` — fixes 0 OWLComp for v14 (96 files), v15 (41), v16 (18).
- **JSPatch member-expr (JS-G2)**: `MyClass.patch("key", fn)` pattern now matched for
  major>=14 era2 extractor — fixes 0 JSPatch for v14-v16.
- **`odoo/osv/query.py` version-aware path (CORE-Q)**: `_resolve_core_paths` maps
  `odoo/tools/query.py` logical path to `openerp/osv/query.py` (v8/v9) or
  `odoo/osv/query.py` (v10-v15) — `class Query` now indexed for all 8 versions.
- **NewId `_V19_CURATED_FILES` entry (V19-G5)**: `odoo/orm/identifiers.py` added so
  `api_version_diff("NewId", 18, 19)` returns moved-not-removed.

### Added / Fixed — WG-3w: writer schema correctness (F-5, F-13, F-8, F-12, arch_snippet, V16-G2)
- **arch_snippet on View nodes**: ~20-30 line excerpt of `<arch>` stored at index time;
  surfaces in `resolve_view` and `model_inspect` output so agents see base view structure.
- **F-5 XML comment-led arch**: `parser_xml.py` skips comment nodes when detecting
  `view_type` from first child — prevents 'form' fallback on comment-led `<arch>`.
- **F-13 USES_FIELD module scoping**: MATCH key includes `module` — eliminates fan-out
  where one `self.X` ref matched Field nodes in every module with that field name.
  Known limitation: cross-module USES_FIELD edges are not generated (same-module-only
  is a precision-over-recall trade-off; see ADR-0034 T5).
- **F-8 USES_FIELD/DEPENDS_ON_FIELD batched tx**: UNWIND batch per method eliminates N+1
  transactions at reindex.
- **F-12 Module MERGE ON MATCH coalesce**: `coalesce($repo_url, m.repo_url)` prevents
  a second-pass write of `repo_url=None` overwriting existing value in multi-repo pool.
- **V16-G2 JSPatch entity_name**: chunk `entity_name` uses patch target class, not
  patch name key, for better semantic search quality.

### Added / Fixed — WG-3t: multi-tenant choke-point (13 leak sites, RELEASE GATE)
- **13 confirmed leak sites closed** (`server.py` + `orm.py` + resources.py) via
  the `_scope` helper + uniform `($allowed IS NULL OR all(...))` guard fragment;
  `profile_name` narrowing is now non-escalating and applied consistently to both
  Neo4j and pgvector paths (eliminates split-brain — see ADR-0034 T2).
- **`tests/test_cross_tenant_isolation.py`** extended to cover all 13 paths (style
  override/resolve, lint xml, api_version_diff, set_active_version probe,
  validate_relation, resolve_view parent, structured variant). Gate must be red when
  any site leaks.

### Added / Fixed — WG-4: query/render correctness
- **F-4 load order** (`_module_dep_closure`): `ORDER BY min_depth DESC` (deepest =
  highest depth number = install first); comment corrected.
- **`<list>` vs `<tree>` view type** (v18+ rename): queries filter
  `v.type IN ['tree','list']` and normalize for render — fixes 0 v18 list views
  returned by `model_inspect` / `find_override_point`.
- **file:line breadcrumb**: `line_start` / `file_path` projected in `find_examples`
  and `model_inspect` render — agents now see source location without a separate lookup.

### Added / Fixed — WG-5: cheap enrichment
- **Edition derive**: `Module.license` → `edition` tag (`CE` / `Odoo EE` /
  `Viindoo EE`) surfaced in `check_module_exists` and `model_inspect` output.
- **Module.summary / description** surfaced in `describe_module` output.
- **OWL field-widget pattern** (`fieldRegistry.add`) added to `patterns.json`.

### Changed — docs / data (this PR, WG-6)
- **`bootstrap_versions.json`**: corrected Bootstrap version + preprocessor for all
  12 versions (v8-v19). Key corrections: v8 BS 3.2.0 (was `3.x`); v9-v11 BS 3.3.5 +
  LESS (v11 was wrong BS-major "4" + SCSS); v12 BS 4.1.3 (was `4.1`); v14 BS 4.3.1
  (was `4.4`); v15 BS 4.3.1 NOT 5 (was `5.1`); v16 BS 5.1.3 (was `5.1`);
  v18/v19 BS 5.3.3 (was `5.3`). `preprocessor` field added; LESS entry-point paths
  corrected for v8-v11. Evidence: source-verified per v*-ground-truth.md S10.
- **ADR-0034**: tenant model clarification amendment (T1-T5) — shared vs own profiles,
  choke-point invariant, cross-process cache 60s constraint, `profile=[]` pre-reindex
  gate, USES_FIELD same-module-only known limitation.
- **ADR-0005**: v10 `__openerp__.py`-only known-miss documented (3 modules:
  l10n_fr_sale_closing, account_cash_basis_base_account, l10n_fr_pos_cert) — Keep
  Simple decision; DualManifestFinder deferred.
- **Reindex runbook**: new §5.11 (multi-tenant pre-traffic gate: profile=[], edition,
  OWLComp/JSPatch v14-v16, Query CoreSymbol, NewId, arch_snippet, cross-tenant leak
  test) + §5.12 (tenant API key ops); 12 new checklist rows.

### Notes
- v18 status: indexer-ready (parser, schema, tools all handle v18). OBS-1 note in
  README updated — the "pending" was only because the v18 repo was not on disk at the
  time of the original note; v18 indexing is fully supported.

## [0.10.0] — 2026-05-23 — Final-stretch: pre-reindex enrichment + agent-convenient output + multi-tenant enforcement gate

One PR (`feat/osm-final-stretch`). Tool surface stays **24** (the module-dependency
capability is a `module_inspect(method='dependencies')` kind, not a new tool). One
Postgres migration (`m13_003`). **OPS follow-up (admin):** after deploy, run the full
reindex v8→v19 — Group A adds new graph/embedding data that is populated only on
re-index. The cross-tenant leak test is the release gate.

### Added — Group A (reindex-forcing graph/embedding enrichment)
- **v19 split-ORM core coverage (A1)** — `parser_odoo_core` resolves the v19 `odoo/orm/`
  package: the `Command` enum keeps its v18 qname `odoo.fields.Command` (via
  `orm/commands.py`, so `api_version_diff` sees a moved file, not a remove+add), plus a
  curated v19 allow-list (`_V19_CURATED_FILES`) for `Domain`/`DomainAnd`/`DomainOr`
  (`orm/domains.py`) and `TableObject`/`Constraint`/`Index`/`UniqueIndex`
  (`orm/table_objects.py`). ~48 internal domain helpers excluded.
- **Neo4j node/edge enrichment (A2)** — `Method.docstring`; `Module.auto_install` /
  `.application` / `.category` / `.external_python` / `.external_bin` (manifest) +
  `.repo_url` / `.repo_id` (repo provenance, threaded pipeline→registry→writer); new
  `(:Method)-[:USES_FIELD]->(:Field)` (direct `self.<field>` access) and
  `(:Method)-[:DEPENDS_ON_FIELD]->(:Field)` (`@api.depends`) edges, best-effort MATCH
  (no stub fields).
- **`Field.string` + `Field.help` (A2-followup)** — field label + help text captured
  (era2 kwarg/positional, era1 best-effort) + persisted + rendered in `resolve_field`.
- **pgvector embeddings provenance (A3) — migration `m13_003`** — `line_start`, `repo`,
  `repo_id` columns; method/field chunks now carry the REAL source `.py` path (was the
  module dir). `parser_xml`/`parser_qweb` switched to lxml for `.sourceline`.

### Added — Group B (agent-convenient tool output)
- **Render existing provenance/intent (B1)** — `resolve_field` (comodel/label/help),
  `resolve_method` (signature/convention), `describe_module` (repo + path),
  `list_js_patches` (file_path), `list_owl_components` (template), `list_fields`
  (ttype/stored/compute/comodel), `find_deprecated_usage` (repo), `validate_domain`
  (did-you-mean typo suggestion).
- **Render new data + module dependencies (B2)** — surfaces docstring / repo_url /
  manifest-deps / embeddings file+line / field-level `USES_FIELD` impact;
  `module_inspect(method='dependencies')` returns the transitive `DEPENDS_ON` closure +
  per-dependency repo + topological load order.

### Added — Group C (multi-tenant enforcement — ADR-0034 WI-3/WI-4, RELEASE GATE)
- **`resolve_tenant_scope(tenant_id)` (C1)** — `(own, shared)` profile sets (own = the
  tenant's profiles; shared = all `tenant_id IS NULL` global base), 60s-cached.
- **Fail-closed Neo4j filter at all 61+4 Cypher sites (C2)** — uniform fragment
  `($own IS NULL OR all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared))`:
  a node is granted iff every profile on it is own-or-shared, so another tenant's
  base-tagged private node is denied and a same-name collision fail-closes. `admin`
  (own=None) stays unrestricted; the optional `$profile_name IS NULL OR` bypass is
  fully removed. `_latest_version` + `find_override_point` now scoped too.
- **pgvector + list-tool scoping (C3/C4)** — `find_examples` / `find_style_override`
  filter `profile_name = ANY(own ∪ shared)` (`suggest_pattern` exempt — global
  catalogue); `list_available_versions` / `list_available_profiles` tenant-scoped.
- **Cross-tenant leak test (C6) — `tests/test_cross_tenant_isolation.py`** — the release
  gate: a tenant sees its own + the shared base, never another tenant's private node
  (with or without an explicit `profile_name`); spec data + admin stay unrestricted.

### Changed
- **ADR-0034 amendment** — records WI-3/WI-4 shipped; documents the pooled MERGE-key
  same-name collision limitation + the operator namespacing convention (proper
  MERGE-key discriminator = deferred REC-8 RFC); D6 Postgres RLS deferred to WI-7
  (the SQL filter is the read-guard; RLS needs `FORCE` + a non-owner read role).
- **`profile_name` is now ADVISORY** (M13 supersedes ADR-0029 "profile is convenience,
  not authz"): the tenant boundary is the isolation mechanism. The pre-M13
  `resolve_view` profile-filter test updated to the new semantics.
- **ADR-0005** corrected (v19 had a residual `Command` gap, now fixed);
  `bootstrap_versions.json` v11 `3.3.4`→`3.3.5`; 4 stale TASKS.md markers de-drifted;
  reindex runbook gains v19/provenance verification queries.

### Notes
- **DEFERRED:** Postgres RLS (WI-7), FERNET secrets manager, M10B Stripe, Prometheus
  histogram, nonce-CSP, VN persona docs + the cross-repo `odoo-mcp-client` mirror for
  `module_inspect(method='dependencies')`.

## [0.9.1] — 2026-05-22 — M13 pre-reindex wave: DB schema + multi-tenant foundation + git integrity

Eight work items (WI-A/B/C/D/E/G/H/I). No new MCP tools; tool surface remains **24**. Two Postgres migrations (`m13_001`, `m13_002`). Admin must run `python -m src.db.migrate` before deploying services, then execute the full reindex runbook.

### Added
- **License policy engine — ADR-0036** (WI-A) — `src/constants.py` `LICENSE_POLICY` config map assigns each license class an action (`serve` / `ingest_flagged` / `skip`). Default: OEEL-1 → `skip` (Viindoo's Odoo SA obligation); copyleft + OPL-1 + unknown → `serve`. `src/indexer/parser_python.py` extracts `license` + `copyright_owner` into `ModuleInfo`; `src/indexer/registry.py` enforces the policy at `build_registry()` (single chokepoint); `src/indexer/writer_neo4j.py` persists `Module.license` + `.copyright_owner` + `.license_notice`. MCP tool output surfaces `license_notice` for skipped/restricted modules — never a silent gap. Config flip (`OEEL-1 → serve`) exposes content with no code change. Test coverage: `tests/test_license_policy.py` (287 lines). Known OEEL-1 modules (skipped by default): v15/v16 — `l10n_it_edi_website_sale`; v17 — `account_payment_term` + `l10n_it_edi_website_sale`; v18 — `certificate`, `l10n_hr_edi`, `l10n_it_edi_website_sale`, `l10n_jo_edi_pos`, `project_hr_skills`; v19 — same minus `l10n_it_edi_website_sale`.
- **`embeddings.profile_name` column — migration m13_001** (WI-B) — `migrations/m13_001_embeddings_profile_name.sql`: `ALTER TABLE embeddings ADD COLUMN profile_name TEXT`; UNIQUE constraint updated; `idx_embeddings_filter` updated. `EmbeddingChunk` dataclass gains `profile_name`; INSERT and per-module DELETE in `src/indexer/writer_pgvector.py` updated. Profile-scoped chunk writes now active. **Postgres RLS deferred** — enforcement (WI-4 choke point) ships in the next enforcement wave. Test coverage: `tests/test_writer_pgvector.py` (142 lines new).
- **`tenants` table + tenant_id FKs + repos uniqueness — migration m13_002** (WI-C) — `migrations/m13_002_tenants_and_fks.sql`: `CREATE TABLE tenants`; `ALTER TABLE api_keys / profiles / ssh_key_pairs ADD COLUMN tenant_id` (FK `ON DELETE CASCADE`, `NULL` = shared/global); `ssh_key_pairs.key_type TEXT CHECK ('deploy_key','access_key')`; `repos` UNIQUE narrowed to `(url, branch, profile_id)` (allows cross-profile duplicates). Backward-compatible — existing rows default `NULL`. Test coverage: `tests/test_db_migrate.py` extended (191 lines total).
- **RelaxNG XML validation → `:LintViolation` nodes** (WI-E) — `src/indexer/parser_xml.py` post-parse step validates each view (v15+) against the version-exact RelaxNG schema read directly from the indexed Odoo source tree at index time (`<core_repo>/odoo/addons/base/rng/<view_type>_view.rng`) — no vendored copy, so every version validates against its own grammar. Correctness is driven purely by file existence: v15-v17 ship `tree_view.rng`, v18-v19 ship `list_view.rng` (Odoo renamed `<tree>` → `<list>`); `<include href>` resolves relative to the same source dir. Errors surface as `:LintViolation` nodes linked via a `(view)-[:HAS_VIOLATION]->(lv)` edge. `lint_check(language='xml')` returns the graph's RelaxNG `:LintViolation` nodes for a version (the `code` argument is not used for xml — this is corpus-level, not snippet-level, linting). Test coverage: `tests/test_relaxng_violations.py` (242 lines) + `tests/test_relaxng_violations_unit.py` (self-contained CI-safe RNG fixtures under `tests/fixtures/rng/`).
- **Git-URL-only repo registration + server-managed `local_path`** (WI-G) — `src/db/repo_registry.py` + `src/web_ui/routes/repos.py`: repos registered by git URL only; `local_path` derived server-side; `tenant_id` FK propagated on creation. Per-profile UNIQUE(url, branch, profile_id) allows the same URL to be registered under different profiles.
- **Known_hosts pinning + strict host checking** (WI-H/WI-9) — `src/git_utils.py`: replaces `StrictHostKeyChecking=accept-new` with a pre-populated pinned known_hosts for GitHub/GitLab/Bitbucket + `StrictHostKeyChecking=yes`. Eliminates TOFU MITM exposure + concurrent known_hosts write race at multi-tenant scale. **MED-2 onboarding constraint:** self-hosted forges require their SSH host key be added to the pinned file as a one-time step. Per-repo Postgres advisory lock (`lock_id` from `repo_id`) wraps every mutating git op (clone/fetch/reset). `git fetch` + `git reset --hard origin/<branch>` refresh path added. Test coverage: `tests/test_git_hardening.py` (487 lines).
- **Self-service deploy-key endpoint** (WI-I/WI-6) — `GET /api/tenant/deploy-key` (`src/web_ui/routes/deploy_key.py`): X-API-Key → tenant_id scoped; returns non-secret public key + add-as-deploy-key instructions; cross-tenant-safe (a key can only read its own tenant's deploy key). Test coverage: `tests/test_tenant_deploy_key.py` (393 lines).

### Changed
- **`verify_api_key` returns `tenant_id`** (WI-D) — `src/db/auth_registry.py` extended; `src/mcp/middleware.py` writes `request.state.tenant_id`; `src/mcp/tool_log_middleware.py` threads tenant context; tool-context thread-local in `src/mcp/server.py` exposes it. Legacy `tenant_id NULL` keys behave as admin/global (only unscoped path). **No read-side filtering yet** — enforcement deferred to WI-3/WI-4. Test coverage: `tests/test_tenant_id_plumbing.py` (397 lines).

### Notes
- No new MCP tools. Tool surface remains **24**. `GET /api/tenant/deploy-key` is a REST endpoint, not an MCP tool.
- **Read-enforcement DEFERRED:** WI-3 (`resolve_allowed_profiles`) + WI-4 (mandatory 61-site filter) + cross-tenant leak-test release gate ship in the next enforcement wave.
- **Verified Cypher site count for WI-4 scope:** 61 user-data Cypher query sites (57 in `src/mcp/server.py` + 4 in `src/mcp/orm.py`) PLUS 3 embeddings queries with no Neo4j filter (`find_examples`, `find_style_override`, `suggest_pattern`). The "~27 sites" figure in ADR-0034 is a pre-survey estimate; correct figure is 61 + 3.
- **OPS follow-up (admin):** `python -m src.db.migrate` to apply m13_001 + m13_002; then run full reindex v8→v19 per `docs/deploy/reindex-v8-v19-runbook.md` (needed for license/copyright_owner backfill + LESS nodes + LintViolation nodes + profile_name backfill on embeddings).

---

## [0.9.0] — 2026-05-22 — Reindex-prep DB-impact wave v8→v19

Bundled under PR #160. Six parser/indexer fixes that require a full reindex v8→v19 to take effect. No new MCP tools; tool surface remains 24.

### Added
- **LESS stylesheet indexing for v8-v11** (WI-3) — `src/indexer/parser_less.py` (regex-based, matching the `parser_scss` approach — no `tree-sitter-less` available on PyPI). Produces `:Stylesheet {language: "less"}` Neo4j nodes, `:IMPORTS` edges for `@import` chains, and `chunk_type='less'` pgvector embeddings (selectors, variables, mixins, imports, raw fallback). `find_examples` and `find_style_override` now accept `less` as a filter. `VALID_CHUNK_TYPES` in `src/constants.py` extended with `"less"`. ADR-0025 addendum added. Test coverage: `test_parser_less.py` (534 lines).
- **Curated `odoo.tools` CoreSymbol coverage — ADR-0033** (WI-4) — 12 `spec_data/tools_symbols_X.0.json` files (v8-v19) with curated `tool_export` CoreSymbols (not auto-parsed — manual curation for accuracy). New `src/indexer/parser_tools_symbols.py` loader. Enables: `lookup_core_api("odoo.tools.SQL","16.0")` = not-available; `"17.0"` = stable. `_DEPRECATED_API_SYMBOLS` expanded from 14 → 19 entries: +4 `image_resize_image*` (removed v13, `image_process` replacement) + `pycompat` (dropped from `odoo.tools.__init__` v19). `safe_eval` dedup: parsed CoreSymbol wins over curated when both exist. Test coverage: `test_parser_tools_symbols.py` + `test_tools_symbols_integration.py`.
- **v8/v9 CLICommand nodes from `parser_cli`** (WI-2) — `parser_cli.py` now resolves `openerp/` paths for v8/v9 (via `_PKG_PREFIX_REGISTRY`, see WI-6 below) and loads the static `commands` array from `spec_data/cli_flags_8.0.json` / `cli_flags_9.0.json` (the `"commands"` key inside each file) to produce `CLICommand` nodes. Test coverage: `test_parser_cli.py` extended with v8/v9 fixtures.
- **Lint rules ≥50/version for v10-v19** (WI-5) — all 10 `spec_data/lint_rules_X.0.json` files (v10-v19) expanded to ≥50 curated rules. `test_lint_rules_minimum_count.py::test_minimum_50_per_version` passes. v8/v9 remain at curation baseline (era1 scarce source data, expected).
- **`VersionRegistry` shared abstraction — ADR-0032** (WI-6) — `src/indexer/version_registry.py`: `VersionRegistry(min_major, max_major|None, handler)` — first-match wins, sorted by `min_major` ascending. Three registries wired: `_ERA_REGISTRY` (parser_python — era1/era2), `_PREFIX_REGISTRY` (parser_odoo_core — openerp//odoo/ prefix), `_OWL_ENABLED_REGISTRY` (parser_js — OWL v14+). `parser_cli` also gets `_PKG_PREFIX_REGISTRY`. Adding Odoo v20 behaviour is a 1-line registry append. Behavior-preserving: all existing era1/era2/era3 tests pass unchanged. OWL guard fails-soft on unparseable/`"unknown"` version (returns `None` = skip) vs prior `int()` which would raise. Test coverage: `test_version_registry.py` (216 lines).

### Fixed
- **v18/v19 generic field classes now classify as `field_type`** (WI-1) — `parser_odoo_core.py` detects `ast.Subscript` (e.g. `Field[int]`, `Field[str]`) in addition to `ast.ClassDef` when classifying CoreSymbols as `kind='field_type'`. Before this fix, v18/v19 generic field classes (`Integer`, `Many2one`, `Char`, etc.) were missing from the CoreSymbol graph after Odoo introduced generic field syntax. Test coverage: `test_parser_odoo_core.py` extended with Subscript fixtures.
- **PR #160 review fixes** — `VALID_CHUNK_TYPES` now includes `"less"` (was missing from initial WI-3 commit); `safe_eval` CoreSymbol dedup: parsed wins over curated (prevents duplicate nodes when both exist); LESS variable regex (`_RE_LESS_VAR`) uses a line-anchored negative lookahead to exclude CSS at-rule keywords (`import`, `media`, `charset`, `keyframes`, etc.) — the lookahead uses `(?![\w-])` so that variable names whose first token happens to start with a keyword prefix (e.g. `@media-breakpoint-xs`, `@page-header-height`) are still captured as variables; `parser_cli` registry wired via `_PKG_PREFIX_REGISTRY` (consistency with WI-6 pattern).

### Changed
- **`bootstrap_versions.json` corrected** (WI-7 docs) — v11 Bootstrap version `"4.0"` → `"3.3.4"` (v11 ships Bootstrap 3.3.4, not 4.x; v11 was the LESS→SCSS/Bootstrap 4 transition version but the actual shipped library is 3.3.4); v17 Bootstrap version `"5.3"` → `"5.1.3"` (precise patch version). The `site/src/pages/bootstrap.astro` page reads this file dynamically and inherits the correction automatically.
- **ADR drift corrections** — ADR-0002 §3 `_DEPRECATED_API_SYMBOLS` count updated 14 → 19; ADR-0025 `language` enum extended to `"css"|"scss"|"less"`, `mixin_count` now documented for LESS too, LESS addendum section added; ADR-0032 Consequences note added for OWL fail-soft robustness.
- **`view_type` docstrings** — `src/mcp/dto.py` `ResolveViewOutput.view_type` + `src/mcp/server.py` `_list_views_core` + `model_inspect`/`module_inspect` Args blocks now mention `'list'` (v18+ tag alias for `'tree'`). No logic change.

### Notes
- No new MCP tools. Tool surface remains 24. No Postgres migration required.
- **OPS follow-up (admin, after deploy):** run the full reindex v8→v19 per `docs/deploy/reindex-v8-v19-runbook.md`. Covers: `index-core` v8-v19 (tools symbols + LESS nodes + CLICommand v8/v9 + lint rules ≥50 + field_type v18/v19 fix); `index-repo --all --full` (LESS nodes + mth.depends backfill); Cypher cleanup (OWLComp pre-v14 + snap_mod); `reembed-stubs` per profile.

---

## [Merged into v0.9.1] — M10C Polish Wave (PR #159)

> **Note:** This block was recorded as `[Unreleased]` before v0.9.1 was cut. All changes
> listed here are included in the `[0.9.1]` release below (shipped untagged — see release
> policy note at the top of this file).

### Added
- **`reembed-stubs` CLI subcommand** (`python -m src.indexer reembed-stubs --profile <name>`) — enumerates modules where `field_count > 0` but `embeddings_count == 0` via `LEFT JOIN embeddings`, re-runs `make_chunks` + `write_module_embeddings`; idempotent; log line summarises count + total embed calls per ADR-0010. (WI-3)
- **`audit-repo` CLI subcommand** (`python -m src.indexer audit-repo --profile <name> --output audit.json`) — emits a per-module JSON coverage report (field count, method count, embedding count, last indexed at) to the path given by the required `--output` flag. Closes M10 Quick Win "CLI batch audit". (WI-3)
- **`GET /api/repos/{id}/core-symbol-counts`** — new FastAPI endpoint returning per-version CoreSymbol counts for a repo; used by the admin UI core-index status column. Auth-gated, admin only. (WI-5)
- **Admin UI "Core Index" column** (`site/src/components/RepoTable.astro`) — per-version CoreSymbol count badge in `/admin/repos`, fetched from the new API endpoint above. Prevents user confusion between "repo indexed" and "core symbols indexed". (WI-5)

### Changed
- **`parser_odoo_core.py` body-level `DeprecationWarning` detection** — method body AST walk (`_has_body_level_deprecation_warning`) now detects `warnings.warn(...)` calls where `DeprecationWarning` appears as any positional arg or as the `category=` keyword (e.g. `warnings.warn("...", DeprecationWarning, stacklevel=2)`). After re-index, `lookup_core_api("name_get", "17.0")` returns `status='deprecated'` instead of incorrect `'stable'`. Detection tightened in review-followup (matches only `warnings.warn`, not `logger.warn`/`self.warn`). (WI-2)
- **`parser_js.py` OWLComp pre-v14 guard** — `_extract_era3_patches` returns early when `major < 14`, symmetric with the existing `_extract_era3_components` guard. Prevents new anachronistic `__unresolved__` OWLComp stubs being written to Neo4j for v8-v13 repos on future reindex. Existing 239 stubs require a one-time Cypher cleanup (see Full Reindex Runbook). (WI-1)
- **`admin_audit_log` legacy column drop** — `actor_id`, `target_id`, `detail_text` columns removed via migration `m9_010_drop_audit_legacy_columns.sql`; dual-write removed from `AuthRegistry.log_audit()` (now canonical-only INSERT). All consumers use the canonical columns `actor`, `action`, `target`, `success` (+ `detail` JSONB via `src.db.audit.write_audit_log`). (WI-4)

### Fixed (review-followup)
- N+1 query hoist in `core-symbol-counts` endpoint - single Cypher query replaces per-version round-trips.
- Neo4j driver close guard in `core-symbol-counts` to prevent connection leaks on error paths.
- Version sort uses `toFloat(v)` in Cypher (not lexicographic) — consistent with ADR-0013 tiebreak policy.
- Migration file renamed `0006_drop_audit_legacy_columns.sql` → `m9_010_drop_audit_legacy_columns.sql` for yoyo ordering consistency.
- Body-level `DeprecationWarning` AST match tightened in `parser_odoo_core.py` to require the callable be exactly `warnings.warn` (`ast.Attribute` `attr=='warn'` with `func.value` Name `'warnings'`) — avoids false positives from `logger.warn`/other `.warn` calls.
- Docstrings corrected for `core_symbol_counts` and `log_audit` to match actual behaviour.

### Notes
- No new MCP tools in this release. Tool surface remains 24.
- **OPS follow-up (admin, weekend):** run `python -m src.db.migrate` to apply `m9_010`; then run full reindex v8-v19 (see Full Reindex Runbook in `docs/deploy/m10-postmerge-ops.md`) to backfill `mth.depends` + correct `name_get` status + clear pre-v14 OWLComp stubs.

---

## [0.8.0] — 2026-05-21 — M10.5 Phase 2: ORM validation tools

### Added
- **`resolve_orm_chain(model, dotted_path, odoo_version)`** — new MCP tool. Walks a dotted field path (e.g. `partner_id.country_id.code`) hop by hop across the indexed Field graph, returning the terminal field type or a `BROKEN` line naming the first unresolved hop. Handles ORM magic fields (`create_uid` → `res.users`, etc.) and inherited fields reached via `INHERITS`/`DELEGATES_TO` (e.g. `message_ids` from a `mail.thread` mixin).
- **`validate_domain(model, domain, odoo_version)`** — new MCP tool. Parses a domain literal and validates each `(field_path, operator, value)` term: every field-path hop must resolve, and the operator must be valid for the version. Operator validity is **version-aware** (cross-version survey v8→v19): `parent_of` from v9, `any`/`not any` only from v17, v19 access-rights variants (`any!`/`not any!`). Logical connectors (`&`, `|`, `!`) are skipped.
- **`validate_depends(model, method, odoo_version)`** — new MCP tool. Reads the indexed `@api.depends('a.b', ...)` arguments of a compute method and validates each dependency path; flags depends on `id` (Odoo raises `NotImplementedError`) and suggests the closest field name for typos. Era1 (v8/v9, no decorator depends) surfaces a clear "no @api.depends" note.
- **`validate_relation(model, field, target_model, odoo_version)`** — new MCP tool. Asserts a field is a many2one/one2many/many2many whose comodel is `target_model` (or a subtype via inheritance); reports the actual comodel on mismatch and suggests the closest field name when missing.
- **`MethodInfo.depends` graph property** (M10.5 Phase 2 data layer) — parser now extracts `@api.depends` string args (era2 AST; lambda/callable args skipped as non-static; era1 has none); writer persists `mth.depends` in Neo4j. Powers `validate_depends`.
- **`valid_domain_operators(odoo_version)` + `RELATIONAL_TTYPES`** in `src/constants.py` — version-keyed domain operator sets; unknown/sentinel versions return a permissive superset (no false positives).

### Changed
- **Tool surface 20 → 24** — four ORM-validation tools added. `tools/list` now reports 24 tools. The four tools read version-tagged graph nodes, so they are version-agnostic; the only version-aware logic is the domain operator set and the era1 depends gate.

### Notes
- Implementation in new module `src/mcp/orm.py` (primitive `_traverse_field_chain` + 4 impls), mirroring `src/mcp/inspect.py` (late-import of `server` to avoid a circular dependency).
- **Ops follow-up:** run `python -m src.indexer index-repo --all --full` on prod to backfill `mth.depends` for existing Method nodes (mirrors the M10.5 Phase 1 `comodel_name` reindex).
- **Cross-repo follow-up:** routing matrix EN+VI + adapters/persona skills for the 4 ORM tools need updating at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) (the client hand-mirrors the server tool surface — no generator).

---

## [0.7.1] — 2026-05-21

### Fixed

- **Superset filter parity:** `model_inspect` now forwards `kind` (method='fields') and `view_type` (method='views') to the underlying enumeration impls; `module_inspect` now forwards `view_type` (method='views'), `bound_model` (method='owl'), and `era` + `target` (method='js'). Completes the filter-forwarding started by `from_module` in 0.7.0 — the supersets now expose every filter the removed flat tools had (ADR-0028).

---

## [0.7.0] — 2026-05-21 — M10A + M10.5-P1: stylesheet tools, magic fields, from_module, noqa, comodel_name

### Added
- **`resolve_stylesheet(module, odoo_version)`** (M10A) — new MCP tool (#19). Returns the full stylesheet chain for a module: file path, import graph, CSS custom properties / SCSS variables. Output follows ADR-0023 tree-grammar contract.
- **`find_style_override(selector_or_variable, odoo_version)`** (M10A) — new MCP tool (#20). Traces which module last re-declares a CSS custom property or overrides a selector across the indexed stylesheet graph.
- **Magic-fields `<builtin>` prelude** (M10A D2) — `resolve_model`, `list_fields`, `resolve_field` now include a synthetic `<builtin>` section listing `id`, `display_name`, `create_uid`, `create_date`, `write_uid`, `write_date` for all `models.Model` subclasses. Source-of-truth: `src/constants.py::MAGIC_FIELDS`. Not written to Neo4j; injected at query time.
- **`from_module` param** (M10A D3) — `model_inspect` (kind=fields) and `entity_lookup` (kind=field) accept an optional `from_module` argument to restrict field declarations to those originating from a specific module.
- **`noqa` suppression in `lint_check`** (M10A D4) — inline `# noqa: <rule_id>` comment suppresses the matching lint rule for that line. Multiple rules: `# noqa: ORM001,ORM002`. Bare `# noqa` suppresses all rules on that line.
- **`Field.comodel_name` graph property** (M10.5 Phase 1) — `FieldInfo.comodel_name: str | None` dataclass field; parser extraction for `fields.Many2one`/`One2many`/`Many2many` (era1 text-regex + era2 AST); writer persists `f.comodel_name` in Neo4j. Enables M10.5 Phase 2 ORM validation tools.

### Changed
- **Tool surface 18 → 20** (M10A D5+D6) — two stylesheet tools added. `tools/list` now reports 20 tools.

### Notes
- PR #156 — includes code-review fixes: model-scoped field dedup, `(none)`-sentinel for missing comodel, hint-variable naming, stylesheet tree-grammar contract + batch Cypher, header decoration for builtin prelude.
- Cross-repo follow-up: routing matrix EN+VI for `resolve_stylesheet` / `find_style_override` needs update at [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client/blob/master/docs/reference/mcp-tool-routing.md).
- M10.5 Phase 1 data layer: run `python -m src.indexer index-repo --all --full` on prod to backfill `comodel_name` for existing Field nodes.

## [0.6.0] — 2026-05-21 — v0.6: remove 10 deprecated flat tools (ADR-0028 timeline)

### Added
- `model_inspect` / `module_inspect` now accept `start_index` + `limit` and forward them to the underlying field/method/view/owl/qweb/js listings — preserves the paginated drill-down that the removed flat `list_*` tools provided (the pager continuation hint now names a superset that actually paginates).

### Removed
- Removed 10 deprecated flat MCP tools (ADR-0028 deprecation timeline): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `list_fields`, `list_methods`, `list_views`, `list_owl_components`, `list_qweb_templates`, `list_js_patches`. Tool surface 28 → 18. Use the `model_inspect` / `module_inspect` / `entity_lookup` supersets instead.

### Fixed
- `resources/read` now honours `set_active_version` — added `on_read_resource` hook to `UsageLogMiddleware` so the sticky per-API-key version applies to `odoo://` resource reads, not just tool calls. [WI-B1]
- `set_active_version` / `set_active_profile` validate inputs — pinning a non-indexed version or unknown profile returns an error tree listing valid options instead of silently falling back. [WI-B2]
- Surviving tools' next-step/pager hints + `TRIGGER/PREFER/SKIP` docstrings no longer reference the removed flat tools — all redirected to the `model_inspect` / `module_inspect` / `entity_lookup` supersets (caught + fixed in-PR by the code-review pass).

### Changed
- ADR-0029 amended: `set_active_profile` documented as default-arg convenience, not an access-control boundary.

---

## [0.5.0] — 2026-05-21 — M10.5 + M11 tool UX · go-live deploy · open-core split · security hardening

Consolidated release covering all work since v0.4.1: the M10.5 + M11 tool-UX/architecture batch, the go-live production deploy, the M9 Coverage Fill + RBAC follow-ups, the open-core repo split with AGPL license metadata, the internal-data security purge, and SPDX/housekeeping. Sub-sections below are grouped by theme and date.

### Housekeeping — SPDX headers + script fix + ADR-0031 (2026-05-21)
- [SPLIT] Housekeeping: added SPDX-License-Identifier: AGPL-3.0-or-later headers to all 200 `tests/**/*.py` and 6 `scripts/` files (`.py` + `.sh`). Fixed `add-spdx-headers.sh` `prepend_py()` to insert SPDX as line 2 when shebang is present (preserves shebang executability). Extended script to cover `tests/`, `scripts/*.py`, and `scripts/*.sh` targets. Added ADR-0031 (python-dotenv auto-load at CLI entry points) to `CLAUDE.md` ADR list.

### Security — purge internal deployment data (2026-05-20)
- [SECURITY] Purged private Viindoo deployment topology (private repo names, seed roster, version presets) from the public repository. Master-data seed roster removed; profiles and repos are now created by admins via the web UI or JSON API. History rewrite applied.

### Open-core repo split + AGPL license metadata (2026-05-20)
- [SPLIT] Moved MIT plugin + client docs to Viindoo/odoo-mcp-client. Server repo retains AGPL-3.0 backend + Astro web UI. Added SPDX-License-Identifier: AGPL-3.0-or-later headers across all 88 `src/**/*.py` files and 42 `site/src/**` files (.ts/.tsx/.astro). Added license field to `pyproject.toml` and `site/package.json`. Added copyright + applicability notice atop `LICENSE`. Added `NOTICE` (Viindoo trademark statement + common_passwords attribution) and `data/common_passwords.txt.LICENSE`.

### Post-0.4.1 hardening + go-live deploy + M9 Coverage Fill + M9 RBAC follow-up (2026-05-18)

6 PRs merged after v0.4.1. Production deployed at PR #119 / commit `3f081b9` (admin-invite signup model active). PR #120 (M9 Coverage Fill) + PR #121 (docs signoff) merged but not yet deployed to prod. Two post-deploy hotfixes shipped 2026-05-18 — PR #124 (`init_pool` ordering in seed_patterns CLI) and PR #125 (CLIFlag null command_name MERGE bug surfaced when running `index-core` against M9 curated spec_data). PR #<TBD> (M9 RBAC follow-up) in progress.

### Migration 0004 self-contained SQL rescue (PR #117)

#### Added
- `migrations/0004_add_missing_version_profiles.sql` seeds all 12 root CE profiles (`odoo_8` through `odoo_19`) with `ON CONFLICT (name) DO NOTHING`. SQL is self-contained for DBA-only rescue paths (no Python required).
- `src/db/seed_master_data.py` remains source of truth for the CE root profiles and still handles 2-pass FK inserts for hierarchical profiles.

#### Tests
- Profile-touching tests migrated to distinct test names (`test_root_99`, `test_mid_99`, `test_leaf_99` at version 99.0) or switched to a seeder-only fixture profile for conflict-test scenarios.
- Seed count assertion in `test_master_data_seed.py` bumped 5 → 12.

### Security headers — CSP + Permissions-Policy (PR #118)

#### Added — closes M9 CSP gap (memory: m9_csp_permissions_policy_gap.md)
- FastAPI `_SecurityHeadersMiddleware` injects `Content-Security-Policy: default-src 'none'` + `Permissions-Policy` on every JSON-API response (ADR-0015 — JSON-only, never serves HTML).
- Astro SSR `_addSecurityHeaders()` emits per-path tighter CSP on every SSR response (`/admin/*`, `/signup`, `/verify-email`, `/reset-password`). `script-src 'self' 'unsafe-inline'` because Astro inlines small page scripts.
- Edge nginx/Caddy emits permissive superset CSP that covers prerendered static pages (`/`, `/pricing`, `/bootstrap`, `/benchmarks`).
- 8 regression tests in `TestSecurityHeadersFastAPI` replace nginx-placeholder `TestNginxHeadersDocumented`.

#### Notes
- Nonce-based CSP migration tracked as M10 followup.

### Go-live batch — writer profile + MFA sync + backup CLI + /api/health (PR #119)

5 commits squashed: 4 WIs (Pattern 1 orchestration) + 1 followup commit (Opus review HIGH fixes + boil-the-lake findings + sanitization). Verified end-to-end on production 2026-05-17 (deploy + post-deploy ops phase). See PR description + `docs/deploy/pre-launch-checklist.md` followups #12-#15 for known gaps.

#### Fixed — WI-1 indexer writer + parser_js + ADR-0016 D7
- `src/indexer/writer_neo4j.py`: 6 placeholder MERGE sites (Module dep, Model INHERITS, Model DELEGATES_TO, View INHERITS_VIEW, QWebTmpl EXTENDS_TMPL, OWLComp PATCHES) now inherit the referencing module's profile array:
  - `ON CREATE SET <node>.profile = $profiles` on first MERGE.
  - `ON MATCH SET <node>.profile = [x IN coalesce(<node>.profile, []) WHERE NOT x IN $profiles] + $profiles` on subsequent MERGEs — UNION semantics mirroring real-node pattern from commit `4ff56a8` (prevents clobber when profile B references a stub previously created for profile A).
- `src/indexer/writer_neo4j.py`: 3 resolver MATCH sites (INHERITS Model, DELEGATES_TO Model, PATCHES OWLComp) now exclude `__unresolved__` stubs via `WHERE NOT coalesce(<var>.unresolved, false)` — symmetric with existing INHERITS_VIEW + EXTENDS_TMPL pattern. Without this, second referencer would resolve INHERITS to first referencer's stub and skip the union write.
- `src/indexer/parser_js.py`: `_extract_era3_components()` returns early when `int(odoo_version.split('.')[0]) < 14` — OWL framework only exists v14+.
- `docs/adr/0016-profile-hierarchy-and-neo4j-isolation.md`: new section **D7 — Stub node ownership policy** documenting the UNION pattern + 6 writer sites + future-contributor guidance.

#### Fixed — WI-2 webui auth MFA sync
- `src/web_ui/routes/totp.py`: `_enable_totp()` and `_delete_totp()` now also `UPDATE webui_users SET mfa_enabled = TRUE/FALSE WHERE id = %s` in the same transaction as the `totp_secrets` write. Login still gates on `totp_secrets.enabled`; users column is now authoritative for queries.
- `migrations/m9_009_backfill_mfa_enabled.sql`: idempotent symmetric reconciliation — sets TRUE for users with `totp_secrets.enabled=TRUE`, FALSE for any user `mfa_enabled=TRUE` without a matching TOTP row. Followup commit added the FALSE-reset half (boil-the-lake F).

#### Added — WI-3 backup CLI + systemd + runbook
- `src/cli.py` `_get_pg_dsn()`: refactored to use `config.from_env_or_ini("PG_DSN", "database", "pg_dsn")` helper (consistent with rest of codebase).
- `src/cli.py` `_resolve_postgres_tool(tool)`: new helper returns `[tool]` if `shutil.which` finds it locally, else `["docker", "exec", "-i", "-e", "PGPASSWORD", container, tool]` (PGPASSWORD forwarded via `-e VAR` syntax — host env propagates into container). Container name from `POSTGRES_CONTAINER` env, default `odoo-semantic-mcp-postgres-1`.
- `src/cli.py` `_resolve_neo4j_tool(tool)`: parallel helper for Neo4j tools (`neo4j-admin database dump`). Container env `NEO4J_CONTAINER`, default `odoo-semantic-mcp-neo4j-1`. No PGPASSWORD bleed.
- `src/cli.py` `_cmd_backup` pg_dump: stdout redirect (`stdout=open(pg_out, "wb")`) instead of `-f <host_path>` so docker-exec'd pg_dump pipes output back to host. psql restore paths already use stdin redirect (no change needed).
- `docs/deploy/odoo-semantic-backup.service` + `.timer` + extended `logrotate.d/odoo-semantic` + bilingual `backup-runbook.md`. Systemd unit uses canonical placeholders (`User=odoo-semantic` + `/opt/odoo-semantic-mcp`) per public-repo convention; `ExecStart` wraps in `/bin/sh -c '... $(date +%Y%m%d-%H%M%S) ...'` so timestamp expands per run (systemd `%` specifiers don't include strftime).
- 4 new docker-fallback tests in `test_backup_cli_docker_fallback.py` + 4 new Neo4j docker-fallback tests in `test_neo4j_cli_docker_fallback.py` + 5 existing CLI tests patched to mock `shutil.which` (environment-sensitive baseline).
- `migrations/m9_007_totp_secrets.sql` stale comment ("no mfa_enabled needed in webui_users") replaced with reference to WI-2 m9_009 sync.

#### Added — WI-4 /api/health auth-exempt endpoint
- `src/web_ui/app.py` `GET /api/health` returns `{"status": "ok", "version": "<__version__>"}` HTTP 200.
- `src/web_ui/middleware.py` `_EXEMPT_EXACT` set includes `/api/health` so unauthenticated requests bypass `AuthRequiredMiddleware`. Loopback-only + security header middlewares still apply.
- `src/_version.py`: new single-source version reader via `importlib.metadata.version("odoo-semantic-mcp")` with `PackageNotFoundError` fallback (no hardcoded duplication of `pyproject.toml`).
- 1 new TestClient test asserting unauthenticated 200 + `status` + `version` keys.

#### Fixed — Followup commit consolidates Opus review HIGH findings + 6 boil-the-lake fixes
- Docker-exec pg_dump no longer writes `-f <host_path>` inside container (loses output). Now uses stdout redirect.
- PGPASSWORD forwarded into container via `docker exec -e PGPASSWORD` (host env override didn't reach pg_dump inside).
- systemd `osm-%%Y%%m%%d-%%H%%M%%S.tar.gz` placeholder fixed: ExecStart wraps `/bin/sh -c '… $(date +%Y%m%d-%H%M%S) …'` (systemd specifiers don't expand strftime; nightly runs now produce distinct files).
- psql call sites switched from `text=True` to bytes mode for consistency with pg_dump fix; stderr decoded with `errors='replace'` for human-readable errors.
- `tests/test_writer_neo4j_stub_profile.py`: module-level `pytestmark = pytest.mark.neo4j` per CLAUDE.md convention; pure-unit OWL era guard test moved to `tests/test_parser_js.py`.
- `_version.py` deduplication (importlib.metadata).
- m9_009 migration symmetric backfill (also resets FALSE for users without active TOTP).
- Neo4j docker-exec fallback (parallel to Postgres helper).
- `src/web_ui/middleware.py` module docstring updated with `/api/health` in exempt-paths list.

#### Tests
- 11 new tests across 4 new files (writer stub profile, MFA sync, backup CLI docker, /api/health) + Neo4j docker fallback tests (post-followup).

#### Sanitization
- Initial commit history had host-specific paths (`/home/<user>/...`) and prod state in PR body; force-pushed to clean 1-commit branch using canonical `/opt/odoo-semantic-mcp` + `User=odoo-semantic` placeholders matching existing `docs/deploy/odoo-semantic-mcp.service`. Memory: `feedback_public_repo_sanitize.md`.

### M9 Coverage Fill batch (PR #120)

7 WIs landed: CSS/SCSS parser, v8 era1 field gap fix, pattern backfill, lint/CLI curation, deferred items absorption.

#### Added
- CSS/SCSS indexing: new `parser_css.py` + `parser_scss.py` with tree-sitter-css backend (regex fallback). Creates `:Stylesheet` Neo4j nodes (composite key `(file_path, module, odoo_version)`) + `:DEFINED_IN` + `:IMPORTS` edges. Pgvector chunk_types `css`/`scss`. (WI-A1, ADR-0025)
- PatternExample catalogue v9-v15: 30 curated patterns from real Odoo sources (`patterns.json` 83→113). (WI-A3)
- LintRule static curation v8-v19: 12 `spec_data/lint_rules_X.json` populated with ~270 rules + schema. (WI-A4)
- CLIFlag static curation v8-v19: 12 `spec_data/cli_flags_X.json` populated with ~880 flags + schema + cross-version deprecation tracking. (WI-A5)

#### Fixed
- v8 era1 `_columns` extraction: string-aware brace scan no longer truncates blocks at `{` inside string literals. `FieldInfo.source_definition` now populated for era1. (WI-A2)

#### Notes
- Post-deploy ops B1-B11 (CoreSymbol/LintRule/CLI ingestion runs, OBS-1 reindex, additional profile registration, full reindex for CSS/SCSS embeddings) tracked in the post-deploy ops plan.
- WI-A7 (deferred items absorption into TASKS.md M10/M10.5/M11 + ADR follow-up sections) pending Opus dispatch.

### Pre-launch checklist signoff (PR #121, docs only)

#### Changed
- `docs/deploy/pre-launch-checklist.md` items §4.1, §5.1, §8.6, §10.5 `/api/health` flipped to `[x]` post PR #119 deploy. §4.2, §5.2 marked `[~]` partial with followup references. §11 sign-off table filled (9 of 11 sections `[x]`).
- Known followups appended: #12 OWLComp v14 anachronism (239 stubs from JSPatch era3 in pre-v14 modules — read-side era guard already protects user output), #13 Neo4j online backup (Cypher export OR Enterprise backup cmd), #14 logrotate `/var/log` perms (pre-existing stanza), #15 §6 tools 15-21 prod smoke (deferred next session).

### Post-deploy hotfixes (2026-05-18)

#### PR #124 — `[FIX] indexer: init_pool before job_store in seed_patterns CLI`
- `src/indexer/seed_patterns.py` now calls `init_pool(dsn, ...)` before resolving `_get_job_store()`. Previous ordering raised `PostgreSQL pool is not initialized` when invoking `python -m src.indexer.seed_patterns --force`, blocking the B10 PatternExample reseed step of the M9 Coverage Fill post-deploy ops sequence.

#### PR #125 — `[FIX] indexer: coalesce CLIFlag command_name null → "server"`
- `src/indexer/parser_cli.py::_load_static_cli_flags` coerces `command_name` `None` → `"server"`, matching the live parser default for `odoo-bin server` flags.
- M9 Coverage Fill curated `cli_flags_*.json` files (12 versions × ~70-88 flags each) declared `command_name: null` for global flags like `--config`, `--init`, `--update`. Neo4j 5.x rejects null property values in MERGE identity keys (`Cannot merge ... null property value for 'command_name'`), aborting every `index-core` invocation before any CLIFlag node was written.
- Regression test covers explicit null, explicit "server", and missing key.

### Documentation

- Closed 4 de-facto-done backlog items in TASKS.md: M11 pattern catalogue target met (113 patterns), lint_json_response.sh advisory clean (0 violations), Reseed Patterns Web UI button verified wired end-to-end, M7.5-P2-SEED production seeding completed in B10 ops phase.
- Deduplicated 9 redundant TASKS.md backlog entries (NAMEGET, v8 era1 CLI, VN translation, pricing, nonce CSP) — each item now lives in exactly one canonical milestone location.
- Split Milestone 10 into M10A (Tool Surface Expansion) + M10B (Billing Wow Core) + M10C (Polish + Observability) for clearer scope.

### Production state at go-live cut (2026-05-18)

- Production HEAD: PR #119 / commit `3f081b9` deployed 2026-05-17 (PR #120 + #121 not yet deployed to prod).
- Neo4j: 0 NULL profile nodes (down from 5,988 pre-cleanup); 0 pre-v14 OWLComp anachronisms among NULL-profile set; 239 `__unresolved__` v8-v13 OWLComp stubs remain (have profile set; tracked as followup #12).
- Backup automation: systemd nightly timer scheduled 03:00:00; first manual run produced 2.55 GB postgres bundle (Neo4j component skipped — followup #13).
- Webui crash sim: passed (SIGKILL → 5s auto-restart).
- Embeddings: 528,577 across all profiles (unchanged from pre-deploy; `--no-embed` verify pass did not touch pgvector).

### M9 RBAC + Key-Ownership Bug Fix (PR #<TBD>)

6 WIs orchestrated (5 code, 1 docs). Root cause: `request.session.get("is_admin")` returned False because login never wrote that field; all 5 legacy API keys had `user_id IS NULL` → admin saw empty list. Additionally closes a security hole (unauthenticated users could not deactivate keys, but any authenticated user could deactivate any key by ID without ownership check) and completes M9 §3.4 admin user management.

#### Fixed
- **API key list filter restored for admins** — new `is_admin_session(request)` helper in `src/web_ui/auth.py` DB-sources `is_admin` per request instead of reading absent session field. Clarifies ADR-0011 rule 6 and prevents regression.
- **API key deactivate endpoint now enforces ownership** — `PATCH /api/api-keys/{id}/deactivate` checks that requesting user owns the key OR is an admin (HTTP 403 if neither). Closes M9 security gap.

#### Added
- **Admin promote/demote** — `PATCH /api/admin/users/{id}/admin` endpoint + UI toggle on `/admin/users` with last-admin protection (refuse demote if it leaves 0 active admins). New `set_user_admin()` AuthStore method.
- **Key→owner attribution** — `owner_username` field on `GET /api/api-keys`; Owner column + "Assign owner" banner on `/admin/api-keys` for legacy NULL-owner keys. New `PATCH /api/admin/api-keys/{id}/owner` endpoint for admin assignment. Self-service UI deactivate on `/account/api-keys`.
- **`/account/api-keys` self-service surface for non-admin users** (slim `AccountLayout`). Non-admins hitting `/admin/*` now redirect to `/account/api-keys` (via Astro middleware). New `/account/index` dashboard (read-only, shows "Profile access: VIEW" status).

#### Architecture
- `is_admin_session(request: Request) -> bool` replaces all `request.session.get("is_admin")` calls. DB-sourced, cached 5 min per existing auth cache.
- Web UI surface split: `/admin/*` for admins (full sidebar); `/account/*` for non-admins (slim sidebar).
- Last-admin protection on demote/deactivate via `set_user_admin()` and `set_user_active()` SQL logic.
- NULL-owner system keys assignable by admins interactively (modal + PATCH).

#### Tests
- 28 new backend + frontend tests (WI-1 through WI-5).

#### Fixed — post-Opus-review follow-ups (committed after PR #127 initial review)
- **browser-tests-admin admin seed**: `set_user_password(TEST_ADMIN_USERNAME, ..., is_admin=True)` — the test admin was seeded with `is_admin=False` (default), causing WI3 middleware to redirect the "admin" browser to `/account/api-keys` and all 70+ admin browser tests to time out (25-min wall clock in CI).
- **ADR-0026 doc drift**: last-admin protection status corrected 409→422 (matches `admin_users.py:285`); `/account/index` described as thin redirect not a dashboard (matches `account/index.astro`); audit action names corrected to `user.set_admin` + `api_key.assign_owner` (matches `@audit_action` decorators).
- **`is_admin_session` fail-closed**: `uid=None` now returns `False` instead of `True`. Malformed session cookie or SessionMiddleware crash no longer grants implicit admin privilege.
- **`set_user_admin` / `set_user_active` concurrent demote serialisation**: added `SELECT ... FOR UPDATE` on the target row before the admin-count check, preventing TOCTOU race where two concurrent demotes could both pass the guard and leave 0 admins.
- **`assign_key_owner_route` audit detail**: old_user_id → new_user_id transition now captured in `request.state.audit_detail` before the PATCH call, giving forensic before/after in the audit log.

#### Docs
- ADR-0026 — RBAC + key ownership (5 design decisions, 2 consequences sections, alternatives considered).
- TASKS.md Stream J (6 WIs + completion note).
- CLAUDE.md new section "Auth — is_admin Source of Truth" (1 paragraph clarifying the DB-sourced rule).
- CHANGELOG.md (this section).

### Tool UX + Architecture — M10.5 + M11 (2026-05-19)

6 waves + 8 patterns landed in a single worktree via the `feat/m10-5-m11-tool-ux-architecture` branch (33 commits over Waves A–F + F-FINAL). Plan: internal plan (archived). Research: 12 MCP design patterns evaluated, 8 adopted (archived internally). 3 new ADRs (0028/0029/0030) + ADR-0023 amended.

### Wave A — Quick Wins (M10.5)

- **Tool annotations** (WI-A1): `READONLY_TOOL_KWARGS = {"read_only_hint": True, "idempotent_hint": True}` applied to all 21 existing `@mcp.tool()` decorators. Signals to MCP hosts that no write side-effects occur. ADR-0023 §2 docstring language policy re-affirmed.
- **Next-step hints SSOT** (WI-A2): centralized into `src/mcp/hints.py` — single dict maps tool name → hint string. All 18 drill-down tools import from there; 4 CI assertions added.
- **Grammar consistency tests** (WI-A3): `tests/test_grammar_consistency.py` — 4 tests (language-policy regex, no-self-loop, truncation-disclosure, next-step-present).
- **Self-mythology docstrings** (WI-A4): `lookup_core_api` and `find_deprecated_usage` TRIGGER/PREFER/SKIP blocks updated with accurate self-description.

### Wave B — Output Envelope (M10.5)

- **Shared TreeBuilder** (WI-B1): `src/mcp/tree_builder.py` — `TreeBuilder` class with `add_branch`, `add_sublist`, `add_next` methods. `_resolve_model` and `_list_fields` migrated as PoC.
- **Pydantic DTOs** (WI-B2): `src/mcp/dto.py` — 6 `*Ref` + 7 `*Output` Pydantic models. `ModelRef`, `FieldRef`, `MethodRef`, `ViewRef`, `ModuleRef`, `PatternRef`; `ModelOutput`, `FieldOutput`, etc.
- **Dual-channel ToolResult** (WI-B3): 7 priority tools (`resolve_model`, `resolve_field`, `resolve_method`, `resolve_view`, `describe_module`, `list_fields`, `list_methods`) return `{"content": tree_text, "structuredContent": dto.model_dump()}`. AI clients that support `structuredContent` get machine-parseable data; others fall back to tree text.
- **Dual-channel tests** (WI-B4): `tests/test_dual_channel_envelope.py` — 8 tests asserting both channels non-empty + DTO schema round-trips.

### Wave C — Drill-down Cohesion (M10.5)

- **Opaque ref IDs** (WI-C1/C2/C3): `src/mcp/refs.py` — per-call ref minter with API-key tenancy + 5min TTL. 6 `_list_*` tools emit `[ref=fN]` row tokens; 4 `_resolve_*` tools accept `target=<ref>` OR canonical `model+field+version` — backward compatible. Pagination: `start_index: int = 0` added to all 6 list tools.
- **Ref drilldown tests** (WI-C4): `tests/test_drilldown_refs.py` — 8 tests (ref lifecycle, cross-tenant isolation, ref→resolve round-trip).

### Wave D — Discriminator Consolidation (M11)

- **3 superset tools** (WI-D1): `model_inspect(target, odoo_version, kind)`, `module_inspect(target, odoo_version, kind)`, `entity_lookup(target, odoo_version)` implemented in `src/mcp/inspect.py`. Discriminator field in `structuredContent` signals which sub-tool was invoked.
- **10 deprecation shims** (WI-D4): `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` + 6 `list_*` tools wrapped with `DeprecationWarning` footer + ADR-0028 migration hint. `@deprecated` decorator in `src/mcp/server.py` adds `[DEPRECATED: v0.5 → v0.6]` prefix to tool description.
- **Tests** (WI-D5): `tests/test_mcp_inspect_router.py` (12 tests) + `tests/test_mcp_deprecation_shims.py` (8 tests).
- **ADR-0028** (`docs/adr/0028-discriminator-consolidation.md`): discriminator field contract, deprecation timeline (v0.5 shim → v0.6 removal), migration guide for callers.

### Wave E — Implicit Context (M11)

- **Session state migration** (WI-E1): `migrations/0005_api_key_session_state.sql` — `api_key_session_state` table with `api_key_id PK`, `active_version`, `active_profile`, `updated_at`.
- **Session module** (WI-E2): `src/mcp/session.py` — `read_session()`, `write_session()`, `normalize_version_arg()`, `resolve_version_v2()`. 60s in-process cache per `api_key_id`. 6 sentinel strings collapse to per-key active version.
- **4 session tools + resolver patches** (WI-E3): `set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles` registered in `server.py`. All 21 existing tool wrappers patched to call `resolve_version_v2` so sentinels work transparently.
- **Session tests** (WI-E4): `tests/test_mcp_session_state.py` — 11 tests (read/write round-trip, sentinel collapse, 60s cache, 24h TTL, concurrent tenant isolation).
- **ADR-0029** (`docs/adr/0029-implicit-session-context.md`): 6 sentinels, 3-tier resolution (explicit → session → latest-indexed), TTL policy, concurrent-tenant isolation guarantee.

### Wave F — MCP Resources (M11)

- **7 resource handlers** (WI-F1): `src/mcp/resources.py` — `register_resources(mcp_instance)` wires `@mcp.resource` for 7 `odoo://` URI templates. LRU cache 1000/300s. Cache key formed from **resolved** version (not raw sentinel) — prevents tenant leakage when two API keys with different active versions read `odoo://auto/model/X`.
- **Top-100 popular models** (WI-F2): `src/mcp/resources_index.py` — `odoo://index/popular_models` resource returns top-100 models by field+method count across all indexed versions; cached 1h.
- **Server wiring + docstring hints** (WI-F3): `register_resources(mcp)` called at startup; 7 `_render_*` functions referenced in their respective tool docstrings as "→ available as `odoo://{version}/kind/...`".
- **Tests** (WI-F4): `tests/test_mcp_resources.py` (6 tests), `tests/test_mcp_resource_cache.py` (5 tests), `tests/test_mcp_resources_auth.py` (4 tests including tenant-leakage regression).
- **ADR-0030** (`docs/adr/0030-mcp-resources-uri-scheme.md`): URI scheme rationale, 7 kinds, MIME-native content negotiation, cache architecture, sentinel handling.

### F-FINAL gate followups

- **Pre-launch checklist** (AC-6): §6 updated to 28 tools, §6.5 added (7 MCP Resources sign-off table).
- **ADR-0023 pagination amendment** (AC-7): `start_index` parameter contract, continuation hint grammar (plain text, not `<error>` tag), `[ref=fN]` row token alignment.
- **README + CHANGELOG** (AC-8): MCP section updated to 28 tools + 7 Resources table; this entry.
- **Tenant leakage fix** (latent bug): All 7 resource handlers now resolve version sentinel before forming cache key; regression test `test_two_keys_different_active_versions_get_their_own_bodies` added to `tests/test_mcp_resources_auth.py`.

---

## [0.4.1] — 2026-05-16 — M9 follow-up: Web UI parity for repo & profile management

5 WIs merged via PR #116.

### Added (M9 follow-up: Web UI parity)

- `PATCH /api/repos/repos/{id}` — edit URL/branch/ssh_key_id/local_path qua Web UI; preserves `head_sha` (incremental indexer compatible). ADR-0024.
- `PATCH /api/repos/profiles/{id}` — edit name/version/description; rejects `name`/`version` change on indexed profiles (HTTP 409 `ProfileIndexedError`); enforces ancestor + descendant version-match invariant (HTTP 422). ADR-0024.
- Admin UI: Edit Repo form, Edit Profile form, profile hierarchy tree view (toggle flat/tree, localStorage persist).
- RepoTable surfaces `clone_error_msg`, `error_msg`, `last_indexed_at` columns.
- Index + Index-All buttons: `--full` checkbox (expose ADR-0007 cleanup flag).
- Audit log captures before/after snapshots for PATCH mutations (ADR-0021 extension).

### Fixed

- TOCTOU race in `update_repo` UNIQUE check — catch `psycopg2.errors.UniqueViolation` → HTTP 409 instead of 500.
- ProfileTree.astro testid clash with flat list (namespaced `profile-tree-*`).
- ProfileTree.astro client-side DOM build → SSR template (Astro convention parity).

### Tests

- +9 backend tests for PATCH endpoints (empty body, single field, indexed guard, ancestor/descendant version match, concurrent UniqueViolation).
- +5 browser tests for tree view toggle and localStorage persistence.

---

## [0.4.0] — 2026-05-15 — M9 "Auth Wow" + M8 cleanup + comprehensive security hardening

19 worktrees merged via 9-phase orchestration. PR #100.

### Added — Auth Wow features

- **OAuth (Google + GitHub)** via `arctic` + `oslo` in Astro SSR. State + PKCE CSRF protection. Account linking on verified email. ADR-0017.
- **Public signup** (`/signup`) with email verification (256-bit token, 24h TTL, single-use), hCaptcha, 3/hour resend rate-limit, HTML-escaped email templates.
- **MFA TOTP** enrollment via `pyotp` with Fernet-encrypted secrets + 10 HMAC-hashed backup codes. Admin user enforced after 7-day grace. ADR-0022.
- **Multi-user admin** (`/admin/users`) — `is_admin` gating, deactivate (revokes sessions), reactivate, reset-password-link (1h TTL token).
- **Tenant API keys** — `user_id` FK scoping; users see only their own keys, admin sees all. `expires_at` filter.
- **Backup CLI bundle** (`.tar.gz`: postgres.sql + neo4j.dump + fernet.enc passphrase-encrypted + manifest.json) + Web UI trigger with SSE log stream. ADR-0018.
- **Restore upload** (`/api/operations/restore`) with full OWASP 10-item checklist: size, content-type, extension, `tarfile.extractall(filter='data')`, disk space, SHA-256 audit, maintenance mode 503, pre-restore safety backup, admin + fresh-MFA (5 min). ADR-0019.
- **Admin audit log** (`admin_audit_log` table) + `@audit_action` decorator + `audit_cli` context manager. 18+ routes covered. ADR-0021.

### Added — Security hardening (30+ findings closed)

- **F1**: Login dummy-hash unconditional bcrypt verify (timing oracle fix — closes username enumeration).
- **F2**: Postgres-backed `login_attempts` rate-limit (multi-worker safe, survives restart).
- **F3**: `TRUSTED_PROXY_CIDRS` env allowlist for `X-Forwarded-For` parsing (prevents IP spoofing).
- **F5**: OAuth `state` + PKCE mandatory.
- **F6**: CSP + Permissions-Policy headers in nginx + Caddyfile parity.
- **F7**: Server-side session store (`active_sessions` table) — instant revoke on logout + session ID rotation on login.
- **F8**: API key hash HMAC-SHA256 (was SHA-256 plain) + 30-day SHA-256 fallback for legacy keys (deadline 2026-06-15).
- **F12**: FERNET startup fail-fast in production if key unset.
- **F13**: `--old-key-env` / `--new-key-env` for `rotate-fernet` (eliminates `/proc/<pid>/cmdline` leak). Atomic rotation with transaction rollback. ADR-0020.
- **F15**: `WEBUI_SECURE_COOKIE` opt-out (`!= "0"` instead of `== "1"`).
- **F20**: `conftest._bypass_webui_auth_for_legacy_tests` now excludes both `test_web_ui_auth.py` AND `test_web_ui_browser.py` (was silent auth bypass).

### Added — DB schema

- 8 new yoyo migrations: `m9_001_oauth_columns`, `m9_002_api_keys_user_fk`, `m9_003_admin_audit_log`, `m9_004_login_attempts`, `m9_005_active_sessions`, `m9_006_email_verifications`, `m9_007_totp_secrets`, `m9_008_key_rotation_log`. `9001_m9_user_mgmt.sql` harmonized as canonical schema.

### Added — UI

- `/admin/users` (list + deactivate + reactivate + reset password).
- `/admin/security` (TOTP enrollment + backup codes).
- `/signup`, `/verify-email`, `/reset-password` (public, prerender=false).
- `/admin/operations` extended: Backup section with SSE log, Restore section with file upload + safety backup display, Migrations read-only display (yoyo `_yoyo_migrations` table), FERNET rotation CLI placeholder.
- `/admin/repos` extended: per-profile parent dropdown (handles 404/422 typed errors from W-RC), "Clone all pending" button + JobStatus wiring, RepoTable SSH key dropdown JS toggle by URL pattern (`git@` → show, `https://` → hide).
- Login page: OAuth "Sign in with Google/GitHub" buttons + MFA step section.

### Added — CLI

- `python -m src.manager` new subcommands: `delete-profile <name>`, `delete-repo <id|url>`, `delete-webui-user <username>`, `list-webui-users`. All deletes require `--yes` or interactive `YES` confirm + write audit log.
- `create-webui-user --admin` flag (bootstraps admin user post-M9 schema where `is_admin DEFAULT FALSE`).

### Added — REST polish

- `POST /api/repos/profiles/{id}/clone-all` returns 404 for nonexistent profile (was 200 "no pending repos").
- `PATCH /api/repos/profiles/{id}/parent` distinguishes 404 (not found) vs 422 (cycle / version mismatch) via typed exceptions (`ProfileNotFoundError`, `ProfileCycleError`, `ProfileVersionMismatchError` in `src/db/exceptions.py`).
- `GET /api/admin/migrations` lists applied yoyo migrations (read-only, admin-gated).

### Added — CI / DX

- Bump `actions/setup-node@v4 → v5`, `pnpm/action-setup@v4 → v5`, `actions/checkout@v4 → v5` (pre-empts GitHub forced Node 24 upgrade — deadline 2026-06-02).
- Replace `python -m jsonschema` with `check-jsonschema` CLI (eliminates DeprecationWarning).
- Add `actionlint` job via `rhysd/actionlint@v1`.
- Top-level `permissions: contents: read` on all workflows (anti-pattern fix).
- `.github/dependabot.yml` for weekly GitHub Actions updates.
- 2 advisory lint scripts: `lint_json_response.sh` (catches `JSONResponse(dict)` missing `_json_safe`), `lint_fetch_content_type.sh` (catches `fetch()` POST/PATCH/DELETE missing `Content-Type` header). Wired into `make lint` as `lint-shell-advisory` (warn-only — 127 legacy JSONResponse violations tracked in backlog for dedicated cleanup PR; lint_fetch_content_type 0 violations).
- New ADRs: 0017 (OAuth), 0018 (backup contract), 0019 (restore upload security), 0020 (FERNET key delivery), 0021 (admin audit log), 0022 (MFA TOTP).

### Changed — Test debt

- Deleted 8 MIGRATED tombstone test files (`test_web_ui_*_browser.py` — coverage moved to `tests/browser/admin/test_repos.py` in M8 W7).
- Fixed httpx per-request cookies + Neo4j session close deprecation warnings (2 of 3 fixed; remaining 1 is documented upstream).
- 656 unit tests + 360 postgres integration tests + 68 neo4j tests pass.

### Operational

- Production runbook `docs/deploy/m9-postmerge-ops.md`: 99.0 test artifact cleanup, index-core v9-v19 re-run, seed-patterns, admin bootstrap, audit log verification, daily cleanup cron (login_attempts, email_verifications, active_sessions).

### Fixed

- `[FIX] indexer: replace urllib with httpx for true wall-clock timeout, fix indexer freeze when embed backend slow/silent`

### Security

- **`site/`: bump `astro` 5.x → 6.x and `@astrojs/node` 9.x → 10.x.** Closes 5 dependabot alerts (CVE-2026-42570 / 45028 / 41067 / 41322 / 29772). Major bump required — Astro 5.x and @astrojs/node 9.x are EOL with no CVE backports.
  - `devalue` pinned to `^5.8.1` via `pnpm-workspace.yaml` `overrides` (transitive — astro 6 still pulls 5.8.0 by default).
  - **Deploy upgrade required:** Node.js ≥ 22.12.0 (was 20+), pnpm ≥ 10 (was 9+). `pnpm-workspace.yaml` now uses `allowBuilds:` + `overrides:` fields (pnpm 10+ format).
  - CI bumped: Node 20 → 22, pnpm 9 → 10 in `.github/workflows/ci.yml`.

## [0.3.0] — 2026-05-14 — M8 "Public Wow"

### Breaking Changes

- **Web UI rewritten as Astro SSR (port 4321 default).** FastAPI dropped all Jinja2 templates and now returns JSON only (port 8003).
  - Deployers must add `odoo-semantic-astro.service` (systemd unit provided at `docs/deploy/odoo-semantic-astro.service`) and run `pnpm build` in `site/` before starting.
  - Nginx config: use `docs/deploy/nginx-m8.conf` — routes `/api/*` → 8003, `/admin/*` + `/` → 4321, `/mcp` → 8002.
  - Direct browser requests to `/api/*` now return `Content-Type: application/json` — no HTML pages served from FastAPI.

### Added

- **Astro 5.x SSR server** (`output: 'server'`, Tailwind CSS, pnpm) in `site/`
- **6 admin pages** SSR-rendered by Astro: login, dashboard, repos, api-keys, ssh-keys, operations
- **AdminLayout** Astro component + Astro middleware session auth (`GET /api/auth/verify` → 401 → redirect `/admin/login`)
- **Landing page** with React Flow `GraphAnimation` island + cinematic 5-frame hero reveal; baked graph snapshot (`site/public/graph-snapshot.json` from `scripts/dump_graph_snippet.py`)
- **Public install page** at `/install/` — Astro SSR, API-key onboarding flow
- **Pricing placeholder page** at `/pricing/` — teaser for M9 SaaS tiers
- **68 browser tests** (Playwright) split across `tests/browser/admin/` (auth-gated flows) + `tests/browser/public/` (landing + install page); 2 parallel CI jobs (`browser-admin`, `browser-public`)
- **ADR-0014** Astro unified UI architecture decision
- **ADR-0015** FastAPI pure JSON API policy
- **ADR-0016** Profile hierarchy + Neo4j Option Y isolation (`parent_profile_id` FK, ancestor array, cycle-free validation) — renumbered from draft 0014 to avoid clash with Astro ADR
- **`_json_safe` helper** (`src/web_ui/utils.py`) for safe `datetime` → ISO string conversion in `JSONResponse` — prevents 500 errors on datetime-bearing objects
- **`/api/jobs/{id}/status` endpoint** extracted to dedicated jobs router (`src/web_ui/routers/jobs.py`)
- **CI Node 20** setup via `actions/setup-node@v4` + `pnpm/action-setup@v3`; `pnpm run check` (TypeScript + Astro type-check) added as required CI gate
- **Auto-seed 26 master data profiles** via `python -m src.db.migrate`: Odoo CE v8–v19, Standard Viindoo v8–v19, Viindoo Internal v17/v18 (48 repos total, `clone_status='manual'`)
- **CLI `seed-master-data`**: idempotent re-seed with `--profiles-only` / `--reset` flags
- **Upgrade runbook** `docs/deploy/master-data-upgrade.md`

### Removed

- All Jinja2 templates (`src/web_ui/templates/*.html`)
- `jinja2` dependency from `pyproject.toml`
- Direct HTML rendering from any FastAPI route

### Fixed (during M8)

- **Astro 5.x `checkOrigin` security:** all mutation fetches in Astro pages now send `Content-Type: application/json` (Astro 5 rejects requests without this header for CSRF protection)
- **Session datetime serialization 500** in `/api/dashboard/stats` and SSH key listing — root cause: `datetime` objects not JSON-serializable in `JSONResponse`; fixed with `_json_safe` wrapper
- **Logout endpoint missing** — `POST /api/auth/logout` added; Astro logout page wired correctly

## [0.2.0] — 2026-05-12

### M7.5 "Persona Wow"

**Track 1 — TRIGGER/PREFER/SKIP docstrings**
- Rewrote all 14 MCP tool docstrings with structured routing blocks (`TRIGGER when:`, `PREFER over:`, `SKIP when:`) so AI clients auto-pick the right tool from natural-language utterances (EN + VN)
- Added `tests/test_mcp_tool_descriptions.py` — enforces all 14 tools have TRIGGER/PREFER/SKIP and descriptions ≤ 1500 chars
- Extended `tests/test_smoke_e2e_mcp_http.py` with stub coverage for 11 previously uncovered tools

**Track 2 — Claude Code plugin package**
- New `dist/odoo-semantic-plugin/` — installable Claude Code plugin with:
  - 11 persona SKILL.md files: CEO (risk-overview, customization-inventory), Developer (override-finder, deprecation-audit, version-diff), Consultant (feature-check, gap-analysis), Marketer (feature-highlights, addon-diff), Sales (capability-proof, objection-handler)
  - 2 sub-agent files: `odoo-router.md` (Haiku classifier) + `odoo-upgrade-planner.md` (Sonnet orchestrator)
  - `/odoo-semantic:connect` slash command for interactive API-key setup
  - `.mcp.json` template with `${ODOO_SEMANTIC_API_KEY}` env interpolation
- New `dist/marketplaces/viindoo/marketplace.json` for self-host distribution
- Added `tests/test_skill_disambiguation.py` — 31/31 parametrized routing accuracy tests (100%)

**Track 3 — Cross-vendor adapters + persona docs**
- New `dist/gemini-gem-instructions.md` — Gemini Gem system instructions with full tool routing for all 14 tools + 5 persona modes
- New `dist/openai-gpt-instructions.md` — Custom GPT instructions with routing rules + OpenAPI Action schema
- New `dist/cursor-rules.md` — Cursor `.cursorrules` with file-type-based auto-triggers for Odoo files
- New `docs/personas/{ceo,dev,consultant,marketer,sales}.md` — 5 EN persona onboarding guides with sample prompts and tool workflows
- Updated `README.md` — added Persona Guides section with cross-vendor adapter links

**Track 4 — Architecture & checklist**
- New `docs/adr/0012-persona-skill-architecture.md` — ADR for TRIGGER protocol + persona skill approach + rejected alternatives
- Extended `docs/deploy/pre-launch-checklist.md` — 11 persona skill sign-off rows in §6

## [0.1.0] — 2026-05-11

- M1–M7 Complete: resolve_model, resolve_field, resolve_method, resolve_view, find_examples, impact_analysis, lookup_core_api, api_version_diff, find_deprecated_usage, lint_check, cli_help, suggest_pattern, check_module_exists, find_override_point
- API key auth + Web UI admin (M5)
- SSH auto-clone, incremental indexer, cross-profile parallel indexing (M6)
- Qualified-name AST scope resolver, yoyo-migrations, Web UI session auth, nightly recall benchmark, go-live docs (M7)
