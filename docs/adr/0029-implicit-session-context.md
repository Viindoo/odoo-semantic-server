# ADR-0029 — Implicit Session Context: Per-API-Key Sticky `odoo_version` + `profile_name`

**Date:** 2026-05-19
**Milestone:** M11 Wave E

---

## Status

Accepted

---

## Context

Every odoo-semantic MCP tool that queries Neo4j or pgvector requires two routing parameters: `odoo_version` (e.g., `"17.0"`) and `profile_name` (e.g., `"viindoo-enterprise"`). Before Wave E, callers had to supply both on every tool call or rely on the `"auto"` sentinel, which silently fell back to `_latest_version()` — effectively hiding the active version from the caller.

Measurement of real session transcripts showed that in a typical 30-call session scoped to a single Odoo version, both parameters were carried redundantly on 80–90% of calls. This creates three concrete problems:

1. **Token waste.** Each `odoo_version="17.0"` and `profile_name="viindoo-enterprise"` argument pair consumes tokens in both the tool-call request and the server-side parameter parsing log. At high tool-call frequency this is measurable (5–15% of the raw argument payload).

2. **LLM parameter drift.** If the user says "switch to Odoo 16" mid-session, the LLM must update its internal state and re-supply the new value on all subsequent calls. Empirically, LLMs hallucinate stale versions after 3–5 tool calls if the parameter is not re-anchored in each response.

3. **Cold-start friction.** A new AI client session must discover what versions are indexed before it can issue its first meaningful query. Without `list_available_versions`, the client either hard-codes a version or uses `"auto"`, risking a mismatch against the actual indexed corpus.

Research across 12 production MCP servers (internal design notes, Pattern 6) shows three prior art implementations:

- **Cloudflare MCP** (`set_active_account`): account ID is stored in a Durable Object and retrieved by `getActiveAccountId()` inside every tool handler. Account-scoped tokens auto-set it from the API key; user tokens require an explicit `set_active_account` call.
- **Azure MCP** (`SubscriptionCommand<T>`): resolves in three tiers — CLI flag → `AZURE_SUBSCRIPTION_ID` env var → reject. Sentinel strings `"subscription"` and `"default"` (common LLM placeholder hallucinations) are treated as empty so the env fallback fires.
- **Sequential Thinking MCP**: server-side history persisted across calls; no explicit session-management API but the same principle of stateful context accumulation.

odoo-semantic's session state requirement differs from Cloudflare's in one critical way: the server is multi-tenant (multiple AI clients share one FastAPI process, authenticated by API key), so session state must be scoped per-API-key rather than per-Durable-Object or per-process. The `api_keys` table already provides the natural tenant boundary.

---

## Decision

### Table: `api_key_session_state`

A new Postgres table stores the per-API-key session context:

```sql
CREATE TABLE api_key_session_state (
    api_key_id   INTEGER     PRIMARY KEY
                             REFERENCES api_keys(id) ON DELETE CASCADE,
    odoo_version TEXT,
    profile_name TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_akss_updated_at ON api_key_session_state (updated_at);
```

Design rationale:

- **PK = `api_key_id`** (not a synthetic surrogate): each API key has at most one active session context row. UPSERT semantics (`INSERT ... ON CONFLICT (api_key_id) DO UPDATE`) are unambiguous without needing a separate unique constraint.
- **`ON DELETE CASCADE`**: revoking an API key (via the admin UI or `DELETE FROM api_keys`) automatically cleans up its session row. No orphan accumulation.
- **`updated_at` indexed**: the TTL check (`updated_at < NOW() - INTERVAL '24 hours'`) uses this index. Session cleanup is lazy (checked on read), not via a background job.
- **`odoo_version` and `profile_name` are nullable**: `NULL` means "not set" — the row exists but no sticky value is active. This allows partial updates (set version without clearing profile) without extra state machinery.

Migration file: `migrations/0005_api_key_session_state.sql`.

### Granularity: per-API-key

Session state is scoped per API key, not per user, not per TCP connection, not per MCP session ID. This matches the granularity of:

- Rate-limit counters (see plan §Appendix B item #9): already tracked per `api_key_id`.
- Usage logs: already grouped by `api_key_id`.
- The `api_keys` table foreign key: the natural join anchor for all per-tenant data.

A single user can hold multiple API keys (e.g., one per project or client environment). Each key maintains independent session state, which is the correct behaviour: an AI client configured with the `viindoo-enterprise-17.0` key should not share state with a client using the `customer-x-16.0` key.

### Resolution order

When an MCP tool needs to resolve `odoo_version` or `profile_name`, the following three-tier order applies at every callsite (implemented in `src/mcp/session.py:resolve_version_v2` and the corresponding `resolve_profile_v2`):

1. **Explicit kwarg** — the caller passed a non-sentinel, non-None value (e.g., `odoo_version="17.0"`). This always wins.
2. **Session state** — `get_session_state(api_key_id)` returns a non-None, non-expired value from `api_key_session_state`. Used when the caller omitted the parameter or passed a sentinel.
3. **Fallback** — `_latest_version()` (for version) or the default profile (for profile). Used when neither of the above provides a value. `_latest_version()` returns `None` when the DB is empty, and the caller surfaces this as a human-readable error ("No indexed versions found — run the indexer first.").

This order is applied at 15 callsites inside `src/mcp/server.py`, replacing the legacy `if odoo_version == "auto": odoo_version = _latest_version()` pattern scattered across each tool handler.

### 24h sliding TTL semantics

A session row is treated as **unset** (equivalent to no row) when:

```
NOW() - updated_at > INTERVAL '24 hours'
```

The TTL is **sliding**: every successful `set_active_version` or `set_active_profile` call updates `updated_at`. A session that is actively used resets its own expiry. A session idle for 24 hours gracefully degrades to the `_latest_version()` fallback rather than returning an error.

The 24-hour window was chosen to match the natural human work cycle: an engineer starting a session in the morning, then resuming after a full day's absence, should not find their version context unexpectedly active. Shorter windows (1h, 8h) were considered but rejected because MCP sessions do not have explicit "close" events — the client simply stops calling tools. A window shorter than a typical working day would cause spurious fallbacks for users who pause and resume across a lunch break.

Cleanup policy: expired rows are **not** deleted by a background job. The `get_session_state` helper checks TTL on read and returns `None` for expired rows. The row remains in the table until overwritten by the next `set_active_*` call or until the API key is deleted (CASCADE). This avoids a scheduled-job dependency and keeps the implementation self-contained.

### Sentinel defense

The following five string values are treated as `None` (not set) at the normalization layer (`src/mcp/session.py:normalize_version_arg`), regardless of whether they appear as the explicit kwarg or are read from the session row:

| Sentinel | Origin |
|---|---|
| `"default"` | Azure MCP prior art; common LLM placeholder |
| `"latest"` | Common LLM synonym for "most recent" |
| `"version"` | Bare key name hallucinated when LLM sees `odoo_version=version` in training data |
| `"any"` | Wildcard hallucination |
| `""` | Empty string from malformed client config |

When a sentinel is detected, the normalization returns `None`, which triggers the next tier of the resolution order (session state → fallback). The sentinels are never written to the database.

This defense mirrors Azure MCP's treatment of `"subscription"` and `"default"` as empty, and extends it with three odoo-semantic-specific values observed in LLM transcripts during internal testing (`"any"`, `""`, and `"version"`).

### 60-second in-memory cache

`src/mcp/session.py` maintains a thread-local in-memory dictionary mapping `api_key_id → (odoo_version, profile_name, fetched_at)`. A cache entry is considered fresh if `time.monotonic() - fetched_at < 60`. Fresh entries bypass the Postgres round-trip on hot-path tool calls.

Cache invalidation is **write-through**: every `set_active_*` call first writes to Postgres, then updates the in-memory entry for the same `api_key_id`. The cache is never the source of truth; it is a read-side optimization only.

**Cross-process staleness limitation:** When the FastAPI server runs under multiple worker processes (e.g., `gunicorn --workers 4`), each worker process maintains its own independent cache. A `set_active_version` call handled by worker A writes to Postgres and updates worker A's cache, but workers B, C, and D continue to serve their stale cache for up to 60 seconds. The worst-case observable behavior is:

> User calls `set_active_version("17.0")` → worker A handles it, writes DB, updates its cache. Next tool call is routed to worker B → worker B's cache has the old value → tool call uses stale version for up to 60s.

This is accepted as a known tradeoff. The 60-second window is short enough to be tolerable in an interactive session. The mitigation available to operators is to configure `--workers 1` or use a sticky-session load balancer in front of the worker pool — both are documented in `docs/deploy.md`. Cross-worker cache invalidation via Postgres `LISTEN/NOTIFY` or a shared Redis layer would eliminate the staleness window but adds operational complexity outside the M11 scope.

### New MCP tools

Four new tools are registered in `src/mcp/server.py`:

| Tool | Description |
|---|---|
| `set_active_version(odoo_version)` | Writes `odoo_version` to session state. Returns a confirmation receipt with the new value and expiry time. Rejects sentinels. |
| `set_active_profile(profile_name)` | Writes `profile_name` to session state. Returns a confirmation receipt. Rejects empty string. |
| `list_available_versions()` | Queries `SELECT DISTINCT odoo_version FROM ... ORDER BY toFloat(...)` (Neo4j 5.x numeric sort). No auth — safe to call at cold start before a key context is established. |
| `list_available_profiles()` | Queries the `profiles` Postgres table for all active profiles visible to the caller. |

`list_available_versions` and `list_available_profiles` are intended for cold-start discovery. An AI client opening a new session should call these first to confirm what versions and profiles are indexed before issuing `set_active_version` and `set_active_profile`.

All four tools carry `**READONLY_TOOL_KWARGS` annotations (Wave A, ADR-0023) with the exception of `set_active_version` and `set_active_profile`, which are `readOnlyHint=False` (they write state) and `idempotentHint=True` (calling twice with the same value produces the same result).

---

## Consequences

### Positive

- **Eliminates 80–90% of redundant parameter tokens** in single-version sessions. An engineer working in Odoo 17.0 for an entire session can call `set_active_version("17.0")` once and omit `odoo_version` on every subsequent tool call.
- **LLM parameter drift eliminated for the common case.** The session value is the authoritative record; the LLM does not need to maintain it in working memory. Only explicit overrides need to be named.
- **Tenant isolation by design.** The `api_key_id` PK ensures no session leaks between API keys. No additional access-control logic is needed.
- **Graceful degradation.** Expired sessions fall back to `_latest_version()` rather than returning an error. AI clients that never call `set_active_*` continue to work exactly as before Wave E.
- **Backward compatibility.** All 15 resolver callsites apply the resolution order transparently. Existing AI client configs that explicitly pass `odoo_version` on every call continue to work unchanged — explicit kwargs always win at tier 1.
- **Cold-start tools are auth-context-free.** `list_available_versions` can be called immediately after establishing a connection, before any session state is set. This removes a chicken-and-egg problem where the client needs to know versions before it can set the active version.

### Negative

- **Cross-process cache staleness** (up to 60s) in multi-worker deployments. Documented in the Decision section above. Operator mitigation: `--workers 1` or sticky-session load balancer.
- **Increased Postgres round-trips on cold-path.** The first tool call after a 60-second cache eviction triggers a `SELECT` on `api_key_session_state`. Under typical session patterns (one `set_active_version` per session, then 20–50 tool calls cached) the overhead is negligible.
- **Four additional tools in `tools/list`.** The tool count grows from 24 (post-Wave-D) to 28 during v0.5.x (before the Wave D deprecated shims are removed in v0.6). The new tools are lightweight discovery and state-management utilities, not graph-query tools — their schema entries are compact.
- **No server-push invalidation.** If an admin re-indexes a profile that changes the available versions, `list_available_versions` will reflect the change immediately (DB query), but a session that has already cached `"17.0"` as active will continue using it until the TTL expires or the user calls `set_active_version` again. This is acceptable — the session state records the user's *intent*, not a snapshot of available data.

---

## Alternatives Considered

### 1. Process-level global state (single `dict` in `src/mcp/session.py`) — rejected

A module-level `_ACTIVE_VERSION: dict[int, str] = {}` keyed by `api_key_id` would eliminate the Postgres round-trip entirely. Rejected because:

- State is lost on every process restart (deploy, crash, `systemctl restart`). For a server under active development this is a nightly event.
- Multi-worker deployments have siloed state from the start, with no DB fallback path.
- Admin key revocation (via the web UI) does not clear the in-process state, so a revoked key could continue to resolve a stale version until the process restarts.

### 2. Cookie or HTTP header (per-request context) — rejected

Passing `X-Odoo-Version: 17.0` as an HTTP header on each MCP JSON-RPC call would avoid any server-side storage. Rejected because:

- The MCP protocol does not define a mechanism for clients to attach persistent headers across tool calls without modifying the AI client config.
- The header approach would require per-client configuration, which defeats the purpose of implicit context.
- It would not survive a session where the LLM itself constructs the tool-call payload — LLMs do not have a reliable mechanism to inject headers into MCP client requests.

### 3. Postgres `LISTEN/NOTIFY` for cross-worker cache invalidation — deferred

Using `NOTIFY api_key_session_changed, '<api_key_id>'` on write and a background listener thread per worker to invalidate the local cache would eliminate the cross-process staleness window. This was evaluated and deferred to a follow-up because:

- It requires a persistent background thread per worker process holding an open `LISTEN` connection to Postgres.
- Thread management interacts with FastAPI's async event loop in non-trivial ways.
- The 60-second staleness window is acceptable for v0.5.x interactive sessions.
- The follow-up can be implemented transparently — the DB schema and the `session.py` API do not need to change.

### 4. Redis session store — rejected

A Redis layer would provide cross-worker cache coherence and sub-millisecond reads. Rejected because:

- Adds a third required infrastructure component (alongside Postgres and Neo4j). The project's deployment guide already documents two services; a third raises the operational bar for self-hosted instances.
- The 60-second Postgres cache already delivers the hot-path latency benefit for the single-worker case, which covers the majority of self-hosted deployments.
- Postgres `LISTEN/NOTIFY` (deferred above) can achieve cross-worker coherence without the additional service dependency.

---

## References

- Internal design notes §Pattern 6 — Implicit context via `set_active_*` tools and env-var fallback (Cloudflare, Azure, Sequential Thinking prior art).
- Internal design spec (Wave E) — per-WI spec; Appendix B item #9 (rate-limit + usage-log granularity = per-API-key).
- `migrations/0005_api_key_session_state.sql` — DDL for `api_key_session_state` table.
- `src/mcp/session.py` — Wave E WI-E2 implementation: `get_session_state`, `set_active_version_db`, `set_active_profile_db`, `normalize_version_arg`, `resolve_version_v2`, `resolve_profile_v2`, 60s in-memory cache.
- `src/mcp/server.py` — Wave E WI-E3: 4 new `@mcp.tool()` wrappers (`set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles`); 15 resolver callsite patches.
- `tests/test_mcp_session_state.py` — Wave E WI-E4: 11 tests covering lifecycle, tenant isolation, sentinel rejection, 24h expiry.
- `docs/adr/0011-web-ui-session-auth.md` — precedent for 8h sliding TTL on web UI sessions; the 24h value for MCP sessions reflects the longer idle periods typical of AI coding tool sessions vs browser sessions.
- `docs/adr/0023-tool-output-completeness.md` — English-only output policy; all four new tools conform.
- `docs/adr/0026-rbac-key-ownership.md` — `is_admin` DB-sourced pattern; `api_key_id`-scoped data follows the same tenant-isolation contract.

---

## Amendment (v0.6, 2026-05-21) — Profile is convenience, not authz

`set_active_profile` injects a default `profile_name` argument for convenience only; it is **not** an access-control mechanism. Any authenticated API key can still query any profile by passing `profile_name` explicitly in a tool call — the sticky profile merely saves callers from repeating a frequently-used default argument.

The profile boundary is **data segmentation**, not authorization. A profile represents a named view of the indexed corpus (e.g., `viindoo-enterprise-17`, `acme-custom-17`), and `set_active_profile` records the caller's preferred segment. It does not restrict which profiles the API key may access.

True per-key profile authorization — where a key is only permitted to query one or more specific profiles — would require:
1. An `allowed_profile_ids` column (or join table) on `api_keys`.
2. Query-level filtering in every resolver (`resolve_version_v2`, Neo4j Cypher `WHERE profile IN allowed`) enforced regardless of whether `profile_name` was supplied explicitly or via session state.

This is out of scope for v0.6. The design decision is to keep the implementation simple until there is a concrete customer demand signal for profile-level authz (e.g., multi-tenant SaaS where different teams must be isolated from each other's indexed codebases). If that signal arrives, the authz layer can be added without changing the existing session-state schema — the `api_key_id` PK already provides the natural tenant anchor.

---

## Amendment (WI-4, 2026-06-02) — `odoo_version` is HARD-REQUIRED on tools (supersedes the sticky-version half)

### What changed

`odoo_version` is now a **required** parameter on every version-bearing MCP tool. Omitting it makes FastMCP reject the call with a `ValidationError` ("Missing required argument") **before** the handler runs, so the model is forced to retry with an explicit, concrete version. This **supersedes the version half** of the original Wave E design (sticky `odoo_version` via `set_active_version` + the `"auto"` default). The **profile half is unchanged** — `profile_name` is still implicit/sticky and convenience-only per the v0.6 amendment above.

### Why (root cause)

The original design let every version-bearing tool default to `odoo_version="auto"`. The 6 sentinels (`auto`, `default`, `latest`, `version`, `any`, `""`) all collapse to `None` in `normalize_version_arg`, after which `resolve_version_v2` walks: Tier-1 explicit → Tier-2 session DB → **Tier-3 `_latest_version()` silently**.

In a long session an LLM reliably *drops* the version argument after a handful of calls (the same "parameter drift" noted in Context §2). With a sentinel default this drift was **silent**: the resolver fell through to the latest indexed version and returned data for the WRONG Odoo version with no signal to the model or the user. For a knowledge engine whose entire value is version-accurate answers, a silent wrong-version answer is worse than an error. Option A (hard-require) converts that silent failure into a loud, self-correcting one.

### Mechanism

Implemented as a shared type alias in `src/mcp/server.py`:

```python
RequiredOdooVersion = Annotated[str, Field(description="REQUIRED — always pass the concrete Odoo version explicitly (e.g. '17.0'); never assume or omit it. Use list_available_versions if unsure which versions are indexed.")]
```

`Annotated[str, Field(...)]` with **no default** is the idiomatic FastMCP/pydantic way to mark a parameter required: FastMCP renders it into the JSON-Schema `required[]` array (verified by introspecting `Tool.parameters` in `tests/test_mcp_tool_descriptions.py`). Because it is syntactically a non-default parameter, on the few tools where `odoo_version` would otherwise follow a defaulted positional param (`cli_help`), the parameter is made keyword-only via `*` to keep valid Python; `entity_lookup` was already keyword-only.

### Scope — 19 tools required, others deliberately exempt

**Required (19):** `find_examples`, `impact_analysis`, `lookup_core_api`, `find_deprecated_usage`, `lint_check`, `cli_help`, `suggest_pattern`, `check_module_exists`, `find_override_point`, `describe_module`, `model_inspect`, `module_inspect`, `entity_lookup`, `resolve_stylesheet`, `find_style_override`, `resolve_orm_chain`, `validate_domain`, `validate_depends`, `validate_relation`.

**Deliberately NOT requiring `odoo_version` (bootstrap / two-version):**
- `list_available_versions`, `list_available_profiles` — take no version (they are how a client *discovers* versions; requiring one would be a chicken-and-egg deadlock).
- `set_active_profile` — sets the sticky profile, no version.
- `set_active_version` — the version *is* its payload (a plain required `str`, not the sentinel-bearing `RequiredOdooVersion` alias); this is how a client bootstraps a session version.
- `api_version_diff` — already requires two explicit versions (`from_version`, `to_version`); `odoo_version` is not one of its parameters.

Tool count is **unchanged at 24** — this is a parameter-schema change only, no tools added or removed.

### Access-path difference — MCP Resources keep sentinel support

MCP **Resources** (`odoo://{version}/...`, ADR-0030) are **NOT** affected and keep sentinel support (`auto`/`default`/`latest` → resolve the API key's active version). The silent-omission failure mode that motivated this change **cannot occur for resources**: the `{version}` segment is a mandatory, structural part of the URI path — a client physically cannot construct a resource URI without supplying *something* in that slot, and a deliberately-passed sentinel is an explicit, auditable choice rather than a silent default.

### Resolver unchanged

`resolve_version_v2` / `_resolve_version` are unchanged. Tier-3 latest-fallback-from-omission is now effectively unreachable for tools (FastMCP rejects omission first), but the resolver still backs (a) resources, (b) the underscore helper functions (`_resolve_model`, etc., which retain their `"auto"` default and are called directly by unit tests), and (c) a tool caller who *deliberately* passes a sentinel string — that still resolves via the session DB as before, preserving backward behaviour for intentional sentinel use. No provenance/disclosure fields were added (Option A alone closes the silent-wrong-version gap).

### Client-repo follow-up (`odoo-mcp-client`)

The client plugin's tool schemas / wrappers must flip `odoo_version` to **required** for the 19 tools listed above (and must NOT require it for the 4 bootstrap/two-version tools). No enum or auxiliary parameter changed — only the `required` flag on the existing `odoo_version` field. Clients that previously relied on omitting the version (letting the server pick latest) must now either pass an explicit version or call `set_active_version` and pass that version on each tool call.

### References

- `src/mcp/server.py` — `RequiredOdooVersion` alias + 19 tool signatures.
- `tests/test_mcp_tool_descriptions.py` — `test_odoo_version_is_required` (19, FAILs on the old `"auto"` default) + `test_odoo_version_not_required_for_bootstrap_tools` (4).
- `docs/adr/0023-tool-output-completeness.md` §2 — English-only parameter descriptions (the `RequiredOdooVersion` description is English-only).

---

## Amendment (#248) — `api_key_id` propagation over stateful streamable-HTTP

**Problem.** With a deliberately-passed sentinel (`odoo_version='auto'`) the resolver was *supposed*
to use the session-pinned version (Tier 2). Over the stateful streamable-HTTP transport it instead
fell through to Tier 3 `_latest_version()`: `set_active_version('16.0')` returned a success receipt
but a subsequent `auto` call answered with the latest indexed version (e.g. 19.0). Reproduced live
on production. This defeats the sticky-session contract for the one access path (deliberate
sentinels) that this ADR kept supported.

**Root cause (pinned).** Inside the offloaded tool body `_get_api_key_id()` read the `'default'`
sentinel instead of the authenticated numeric PK. `AuthMiddleware` (a Starlette
`BaseHTTPMiddleware`) writes `request.state.api_key_id` on its per-call request, but the MCP stateful
session manager runs the tool body in a **long-lived `Server.run` loop task** and exposes the
per-call request to the FastMCP hooks via the MCP `request_ctx` bridge. The `scope["state"]`
mutation `AuthMiddleware` made does **not** survive that
BaseHTTPMiddleware↔session-manager↔`request_ctx` boundary, so
`UsageLogMiddleware.on_call_tool`/`on_read_resource` read `None` and wrote `'default'` into
`_api_key_id_var`. Then `set_active_version_db`/`get_session_state` silently no-op on a non-numeric
id → no persist, no read → Tier-3 latest. The success receipt was emitted unconditionally — a lie.

> The earlier hypothesis that `on_call_tool` lacked the `on_read_resource` ContextVar fix is
> superseded: both hooks already carried identical code (PR #197). The defect is the **source value**
> (`request.state.api_key_id` empty), not the hook plumbing — so a fix that merely re-set a ContextVar
> in `AuthMiddleware.dispatch` would NOT reach the loop task and was rejected.

**Fix (header-fallback, robust-by-construction).** `scope["headers"]` is set by the ASGI server and
untouched by middleware, so `X-API-Key` always survives on the per-call request, and `AuthMiddleware`
has already populated its warm `_KEY_CACHE`/`_TENANT_CACHE` for that key before the hook fires. In
`on_call_tool`/`on_read_resource`, when `request.state.api_key_id is None`, recover the numeric PK
from the header via `middleware._cache_get`/`_cache_get_tenant`
(`tool_log_middleware._recover_identity_from_header`). This single source-repair fixes every
downstream `_get_api_key_id()` consumer at once — version pin, profile pin, `odoo://auto/...`
resources, and the usage/audit/tenant-attribution call sites that were also mis-attributed to
`'default'`. On a cache miss (TTL edge) the prior graceful `'default'` fallback is kept (no
regression). `'default'` remains the legitimate value for stdio/CLI/no-auth (no `X-API-Key`).

**Honest receipts.** `set_active_version_db`/`set_active_profile_db` now return `bool`
(`True`=persisted). The tools emit a success receipt only when persisted; on a skipped write they
fail **loud** when an `X-API-Key` header is present (authenticated HTTP — a real propagation failure)
and emit a gentle "no-op on this transport" note otherwise (stdio/CLI). The silent `.debug` skip is
now `.warning`.

**Tests.** `tests/test_mcp_session_header_fallback.py` drives the real hook → `_api_key_id_var` →
`_get_api_key_id()` inside `call_next`, faithfully simulating the state-loss; RED without the fix
(`'default'`), GREEN with it, plus negative controls (cold cache → graceful `'default'`; no header →
stdio path; state-present → no fallback). `tests/test_mcp_session_receipt_honesty.py` guards the
three receipt branches. (The end-to-end state-loss is empirically confirmed on prod; a full
uvicorn-socket stateful-handshake test is a possible hardening follow-up.)

**Receipt wording aligned.** The `set_active_version` *success* receipt previously read "calls that
omit `odoo_version=` will resolve to this version" — obsolete after this same ADR's required-version
amendment (omission is now a validation error on the 19 tools) and non-functional under #248. It now
reads "pass `odoo_version='auto'` to reuse this pin", which is accurate under both the required-version
rule and the restored sticky resolution. (`set_active_profile` is unchanged: `profile_name` is NOT in
the required set, so omission remains valid for the profile dimension.) This closes the
surface-description point raised on `Viindoo/odoo-mcp-client#38`.

**Pinned-stack non-reproduction (honest scope).** The state-loss was confirmed live on production, but
does NOT reproduce under the currently pinned `mcp 1.27.0 / fastmcp 2.14.7 / starlette 1.0.0` stack in
a local real-socket harness — there `request.state.api_key_id` survives into the tool body. The
header-fallback is therefore a **defense-in-depth recovery**: it activates precisely on the prod
topology that drops scope-state and is a no-op where state already propagates, so it is safe under any
stack. Consequently a real-socket RED→GREEN test is not achievable on the pinned stack; the guard is
the hook-level `tests/test_mcp_session_header_fallback.py` (which injects the state-loss precondition
and is RED without the recovery). Revisit a socket-level isolation test only if a future
mcp/fastmcp/starlette bump reintroduces the loss.

**References.** `src/mcp/tool_log_middleware.py` (`_recover_identity_from_header`),
`src/mcp/session.py` (`set_active_*_db` → `bool`), `src/mcp/server.py` (`_http_request_has_api_key` +
receipt branches). No migration. Tool count stays **24**.

---

## Amendment (#251) - per-session keying + profile read path

### The bug

The sticky pin was keyed by `api_key_id` **alone**. A single API key is the
normal granularity for a person or a project (per the original "Granularity:
per-API-key" decision above), but in practice one person runs **several Claude
Code sessions concurrently on the same key** - different terminals, different
repos, different Odoo versions. With a single pin row per key, those sessions
clobbered each other: `set_active_version("16.0")` in session A and
`set_active_version("17.0")` in session B raced last-write-wins, and any call
that resolved through the pin (a deliberate `odoo_version="auto"`, or any
`profile_name`-omitting call) in session A could silently pick up session B's
version/profile. For a version-accurate knowledge engine that is a
silent-wrong-answer, the same failure class the WI-4 amendment closed for
omission - here re-opened by concurrency.

### The fix - key by `(api_key_id, mcp_session_id)`

The pin is now keyed by the composite `(api_key_id, mcp_session_id)`. Each live
MCP session gets its own pin entry, so concurrent sessions on one key never
collide. The `mcp-session-id` is read at **tool-body time** from the per-call
request headers - #248/#250 established that body-time header reads are reliable
over the stateful streamable-HTTP transport (the `X-API-Key` / `mcp-session-id`
headers in `scope["headers"]` survive the
BaseHTTPMiddleware↔session-manager↔`request_ctx` boundary that `request.state`
does not). When no session id is available (stdio / CLI / header-less callers)
the resolver falls back to the sentinel bucket `_nosession`, which reproduces the
pre-#251 single-pin-per-key semantics byte-for-byte for those transports.

### Storage = in-memory (NOT a DB migration)

The pin store is a single in-process dict guarded by one `threading.Lock`; it is
the **source of truth** for the pin. There is no migration, and the
`api_key_session_state` Postgres table (created in the original decision) is now
**vestigial - no longer read or written**, but kept (not dropped) to avoid an
irreversible schema change for a still-young table.

Rationale for moving off Postgres rather than adding an `mcp_session_id` column:

- **Zero DB I/O on the hot path.** At thousands of concurrent users, each with
  several live sessions, a per-`(key, session)` pin row that is read on every
  resolving tool call would add Postgres round-trips to the busiest code path.
  An in-memory dict get/put is O(1) with no I/O under the lock.
- **A session-keyed PG row is dead-on-restart anyway.** An `mcp-session-id` is
  ephemeral - it lives in the transport's in-process state and dies with the
  process. A persisted row keyed by a session id that no longer exists (the id
  404s on the next request and the client re-mints one) is unreachable garbage;
  persisting it buys nothing.
- **Production is single-process-async with no Redis.** There is no second
  worker to share state with and no shared cache layer in the deployment, so the
  cross-process-staleness tradeoff that justified the Postgres write-through in
  the original design no longer applies.
- **Bounded + self-cleaning.** The store is size-capped at
  `MCP_SESSION_PIN_MAX` (env, default 50000) with oldest-by-`set_at` eviction, so
  thousands of sessions cannot grow it unboundedly, and a 24h idle TTL (applied
  in memory at read time) ages out abandoned sessions.

### Profile read path now wired (narrowing-only, fail-closed)

The profile read path - previously dead because `resolve_profile_v2`'s result was
never consumed downstream - is now **wired**. `_resolve_profile` proposes the
per-session pinned profile (from `set_active_profile`) when a tool omits
`profile_name`, and that proposal is injected at the **top** of both `_scope`
(Neo4j tenant array-filter) and `_effective_allowed` (pgvector single-value
filter), *before* the existing ADR-0034 tenant narrowing.

The injection is strictly **narrowing-only** and **re-validated at READ time**
through the existing ADR-0034 tenant choke:

- The pinned profile can only shrink the visible set *within* `own ∪ shared`; it
  can never widen it nor cross tenants.
- A scoped tenant whose pin is **out of scope** (not in `own ∪ shared`) gets
  deny-all (`_scope` → `own=[], shared=[]`; `_effective_allowed` → `[]` → GUC
  `''` matches nothing) - **fail-closed**, never fail-open.
- An admin (tenant `own=None`) stays **unrestricted**; the pin only narrows as a
  convenience.

Crucially, **no new per-key `allowed_profile_ids` authz column was added** - the
larger profile-authorization design the v0.6 amendment deferred stays deferred.
This change only un-defers the narrow piece: read the *already-recorded
convenience default* through the *already-existing* tenant gate. The pin remains
data-segmentation convenience, not authorization; authorization is still the
ADR-0034 tenant boundary doing the enforcing.

Note the **WI-4 asymmetry** that makes this worthwhile: `odoo_version` is
hard-required on the 19 version-bearing tools, so the version pin only ever
serves a *deliberate* `odoo_version="auto"`; but `profile_name` was deliberately
left **optional** (it is not in the required set), so the profile read path serves
the *normal omit path* - the higher-impact half of the implicit-context promise.

### Durability honesty

Session pins are ephemeral. They **reset on server restart** (the
`mcp-session-id` lives only in the transport's in-process state) and are
**idle-evicted** after 24h (or sooner under `MCP_SESSION_PIN_MAX` pressure).
After a restart or eviction, clients simply re-run `set_active_version` /
`set_active_profile`, or pass explicit versions/profiles. This is the intended
contract, not a regression - a pin records the caller's *intent for this live
session*, not durable state.

### References

- `src/mcp/session.py` - `(api_key_id, mcp_session_id)`-keyed in-memory store,
  `_NO_SESSION_SENTINEL` (`_nosession`) fallback, `MCP_SESSION_PIN_MAX`
  oldest-evict, 24h in-memory idle TTL, `get_session_state` /
  `set_active_version_db` / `set_active_profile_db` / `resolve_version_v2` /
  `resolve_profile_v2` all taking `mcp_session_id`.
- `src/mcp/server.py` - `_get_mcp_session_id`, `_resolve_profile` (proposes the
  pinned default), `#251` injection at the top of `_scope` and
  `_effective_allowed` (narrowing-only, fail-closed re-validation via the
  ADR-0034 choke).
- No migration. `api_key_session_state` is vestigial (kept, not dropped). Tool
  count stays **24**.

---

## Amendment (#274) — Canonical pattern under concurrency: explicit-per-call

### The requirement

Two concurrency shapes must resolve the **right** Odoo version / profile for each
actor, with no silent cross-contamination:

- **R1 — multi-version in one flow.** A single session legitimately needs more
  than one version at once: a version-diff (v16 vs v17), an upgrade walk-through
  (v16 → v17), or a migration chain (v15 → v16 → v17). Each step targets a
  different version *within the same live MCP session*.
- **R2 — concurrent sub-actors, one key, one session.** Several Claude Code
  subagents fan out under one API key. Over the stateful streamable-HTTP
  transport they can share **one** `mcp-session-id` (the parent's), each working a
  different version (principal on v19, sub-agents on v17 / v18). Each must resolve
  its own version.

### Why the pin alone cannot satisfy this

The #251 pin is keyed `(api_key_id, mcp_session_id)`. That fixes the *parallel
sessions* case (distinct `mcp-session-id`s never clobber). But it does **not**
distinguish two concurrent actors that **share one** `mcp-session-id`: the server
sees one pin slot, and a deliberate `'auto'` / sentinel reflects *whoever pinned
last* (last-write-wins). For R1 a single session physically has one pin at a time;
for R2 sub-actors under a shared id share that one slot. So **`'auto'` is a
single-actor convenience, never a concurrency-safe selector.**

### The canonical contract — pass an explicit `odoo_version` (and `profile_name`) per call

The resolver is **race-free by construction** for both shapes because **Tier-1
(explicit) ALWAYS wins over the pin** — the pin (Tier-2) and latest fallback
(Tier-3) are read *only* when the explicit arg is `None`/sentinel
(`resolve_version_v2`, `resolve_profile_v2` in `src/mcp/session.py`). Therefore:

- **R1 (multi-version flow):** pass the concrete version on each step
  (`odoo_version="16.0"` then `"17.0"`). The pin is irrelevant; no step can be
  poisoned by another. This dovetails with the WI-4 amendment above, which makes
  `odoo_version` **hard-required** on the 19 version-bearing tools — the model is
  *forced* to name a version, so the common R1 path is correct by default.
- **R2 (concurrent sub-actors):** each actor passes its own explicit version on
  every call. Sharing one `mcp-session-id` is then harmless — the pin is never
  consulted, so last-write-wins on the shared slot cannot affect any actor that
  passes explicit. `'auto'` is reserved for a true single-actor session that
  deliberately opts into the convenience.

**Profile is symmetric.** An explicit `profile_name` is Tier-1 and likewise wins
over the pin, so concurrent actors needing distinct profiles pass `profile_name`
per call. The crucial extra guarantee for profile is **authz**: the profile pin is
data-segmentation convenience, *not* authorization (v0.6 amendment), and the #251
profile read path is re-validated at read time through the ADR-0034 tenant choke
(`_scope` / `_effective_allowed`). So even a stale or wrong pinned profile under
concurrency can only ever **narrow** within `own ∪ shared` or deny-all
(fail-closed) — it can never widen access or cross a tenant boundary. A
concurrency race on the profile pin is thus a *convenience* defect (a narrower-
than-intended view, self-correcting by passing `profile_name`), never a
*security* defect.

### Fail-loud, not silent-default (R-A2)

When a sentinel / omitted version has **no resolvable pin AND the index is empty**,
`resolve_version_v2` raises a **loud, actionable** `ValueError` naming the explicit
`odoo_version=` contract, rather than inventing a default. The **deliberate-
sentinel-to-latest** fallback is preserved for its documented consumers (Resources
`odoo://auto/...` per ADR-0030; the WI-4/#248 backward-compat path; the underscore
helpers called directly by unit tests) — a non-empty index resolves `'auto'` to the
latest indexed version as before. Any *policy* that would reject `'auto'`-with-no-pin
on the **tool** path (vs. the Resource / helper / backward-compat path) belongs at
the tool-body layer (`server.py`, alongside the `RequiredOdooVersion` schema), not
in the shared resolver — the shared resolver must keep the fallback that Resources
and deliberate sentinels depend on.

### Scope of this amendment

Documentation + resolver-message hardening only. `resolve_version_v2` /
`resolve_profile_v2` keep their Tier-1-always-wins ordering (no behaviour change for
explicit or for deliberate sentinels); the empty-index `ValueError` message is made
explicit about the per-call contract. No new parameter, no migration, tool count
stays **24/25**.

### References

- `src/mcp/session.py` — `resolve_version_v2` / `resolve_profile_v2` docstrings
  (explicit-Tier-1-wins + single-actor-pin notes) and the hardened empty-index
  `ValueError`.
- `tests/test_mcp_session_pin_clobber.py` — N-actor matrix: explicit overrides the
  pin under shared-`mcp_session_id` last-write-wins (version + profile); distinct
  sessions stay isolated; sentinel-with-no-pin-and-empty-index fails loud.
- WI-4 amendment (above) — `odoo_version` hard-required on 19 tools makes the R1
  common path explicit by default.
- ADR-0034 — the tenant choke that re-validates a pinned profile (narrowing-only,
  fail-closed), making a profile-pin race a convenience defect, not a security one.
- ADR-0030 — Resources keep sentinel support; the reason the shared resolver must
  retain its latest-fallback.

---

## Amendment (#279, 2026-06-11) — Option D (`context_id` per-call keying) evaluated and declined

### Decision

**Option D is closed WONTFIX.** Keying the session pin by `(api_key_id, mcp_session_id,
context_id)` was evaluated during issue #279 and declined.

### Reasons

1. **Version race is fully resolved.** The `RequiredOdooVersion` hard-require (WI-4 amendment
   above) forces an explicit, concrete version on every tool call. No pin read occurs on the
   19 version-bearing tools when the caller provides an explicit version (Tier-1 always wins).
   There is no version-race surface left to protect with `context_id`.

2. **Profile race is authz-safe and documented.** The profile pin race is a *convenience* defect,
   not a security defect: the ADR-0034 tenant choke re-validates the pinned profile at read time
   (narrowing-only, fail-closed). A stale or wrong pin under concurrency can only produce a
   narrower-than-intended view; it can never widen access or cross a tenant boundary. The
   mitigation is documented: pass `profile_name` explicitly per call.

3. **No concrete customer signal.** No bug report of "concurrent same-session subagents showing
   data from the wrong profile" has been received.

4. **Fundamental mechanism gap.** MCP protocol has no per-call header mechanism that an LLM
   subagent can use to inject a `context_id`. Delivering it would require either (a) adding
   `context_id: str | None = None` as a parameter on all 19 tools (schema noise, confusing UX)
   or (b) a custom `_meta` field without FastMCP support. Neither is acceptable at this cost
   for a theoretical defect with no customer signal.

### Scope

No code change. Documentation only (this amendment + CHANGELOG Known caveats). Tool count stays
**25**. No migration.

### Reopen triggers

Reopen this issue if any of the following occur:

1. **Customer bug report** of concurrent same-session subagents showing data from the wrong
   profile ("subagent A saw profile X data instead of profile Y").
2. **Roadmap feature** that explicitly supports multi-subagent isolated-profile workflows where
   different subagents in one session need distinct, non-interfering profile contexts.
3. **MCP protocol** adds a native `_meta.context_id` field or equivalent per-call context
   mechanism - implementation would then be trivial (~50 LOC server, no client schema change).

### References

- Issue #279 — full investigation notes + DEBATE.md + deep-2-context-id-pin.md.
- ADR-0034 — tenant choke (narrowing-only, fail-closed) that makes the profile race authz-safe.
- Amendment (#274) above — `RequiredOdooVersion` hard-require that closed the version race.
- `odoo-mcp-client` `skills/_shared/concurrency-guard.md` — client-side SSOT for the
  explicit-per-call rule (version); profile-explicit rule is a tracked follow-up.

---

## Amendment (ADR-0051, 2026-06-18) — 6 additional RequiredOdooVersion tools (19 -> 26)

ADR-0051 (test-surface index) added 6 new MCP tools, all carrying `RequiredOdooVersion`:
`find_test_examples`, `tests_covering`, `test_class_inspect`, `test_base_classes`,
`test_coverage_audit`, `js_test_inspect`.

Total tools with `RequiredOdooVersion` is now **26 of 31**. The 5 exempt tools
(`list_available_versions`, `list_available_profiles`, `set_active_version`,
`set_active_profile`, `api_version_diff`) are unchanged. The concurrency-guard rule
(explicit-per-call) extends unchanged to all 26 version-requiring tools.
