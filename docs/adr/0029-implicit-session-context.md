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
- `/home/tuan/.claude/plans/rippling-greeting-tulip.md` §5 Wave E — per-WI spec; Appendix B item #9 (rate-limit + usage-log granularity = per-API-key).
- `migrations/0005_api_key_session_state.sql` — DDL for `api_key_session_state` table.
- `src/mcp/session.py` — Wave E WI-E2 implementation: `get_session_state`, `set_active_version_db`, `set_active_profile_db`, `normalize_version_arg`, `resolve_version_v2`, `resolve_profile_v2`, 60s in-memory cache.
- `src/mcp/server.py` — Wave E WI-E3: 4 new `@mcp.tool()` wrappers (`set_active_version`, `set_active_profile`, `list_available_versions`, `list_available_profiles`); 15 resolver callsite patches.
- `tests/test_mcp_session_state.py` — Wave E WI-E4: 11 tests covering lifecycle, tenant isolation, sentinel rejection, 24h expiry.
- `docs/adr/0011-web-ui-session-auth.md` — precedent for 8h sliding TTL on web UI sessions; the 24h value for MCP sessions reflects the longer idle periods typical of AI coding tool sessions vs browser sessions.
- `docs/adr/0023-tool-output-completeness.md` — English-only output policy; all four new tools conform.
- `docs/adr/0026-rbac-key-ownership.md` — `is_admin` DB-sourced pattern; `api_key_id`-scoped data follows the same tenant-isolation contract.
