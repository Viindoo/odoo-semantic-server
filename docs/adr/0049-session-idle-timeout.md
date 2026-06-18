# ADR-0049 — Transport-layer session_idle_timeout via Option B http_app bypass (#279)

**Status:** Accepted
**Date:** 2026-06-11
**Authors:** Engineering team
**Related:** ADR-0029 (implicit session context — the version/profile PIN TTL is a
  SEPARATE 24h write-anchored layer, NOT this transport timeout), ADR-0046 (MCP
  embed concurrency / anti-hang — same "fail-safe under disconnect" posture),
  ADR-0031 (python-dotenv auto-load — SESSION_IDLE_TIMEOUT is read post-dotenv at
  the main() entry point)

---

## Context

The MCP server runs over FastMCP's streamable-http transport. Each live MCP
session is held in the MCP SDK's `StreamableHTTPSessionManager._server_instances`
dict, each entry pinning anyio memory streams + one running task + a CancelScope
(~100-200 KB per session, dominated by the live task overhead).

A well-behaved client closes its session with an HTTP `DELETE`. An AI client that
simply disconnects (TCP drop, process kill, network blip) without `DELETE` leaves
its session entry resident. The MCP SDK supports reaping these via
`StreamableHTTPSessionManager(session_idle_timeout=...)` — an idle CancelScope
deadline pushed forward on activity and auto-cleaning up after the idle window.

**The problem:** FastMCP does NOT forward `session_idle_timeout`.
- `mcp.http_app()` / `create_streamable_http_app()` in FastMCP 2.14.7 (pinned,
  `fastmcp>=2.3,<3.0`) build the session manager WITHOUT the kwarg.
- FastMCP v3.4.2 (latest main) still omits it.
- The community fix, PrefectHQ/fastmcp PR #3443, is `open` + `dirty` (merge
  conflict) + labelled `DON'T MERGE`, and targets v3 (outside our pin). Its
  blocker (MCP SDK 1.27.0) cleared 2026-05-20 but the label has not been removed.

So with no upstream path, abandoned streamable-http sessions persist until the
MCP process restarts. The service runs `Restart=on-failure` (no scheduled
restart), so in practice the leak is bounded only by uptime between deploys
(weeks). Worst case (~100 disconnecting clients/day): ~450 MB/month on an 8 GB
host — a resource leak, not data loss, but real.

The MCP SDK 1.27.0 (installed) `StreamableHTTPSessionManager.__init__` DOES accept
`session_idle_timeout: float | None`. The mechanism is upstream-tested. We only
need to get the kwarg to the constructor.

## Decision

**Adopt Option B: bypass `mcp.http_app()` and build the streamable-http
Starlette app directly in `main()`, reproducing FastMCP's
`create_streamable_http_app()` plus four additions over the bare factory:
(1) the `session_idle_timeout` kwarg; (2) forward `json_response` and
`stateless_http` read off `mcp._deprecated_settings` so an operator using
`FASTMCP_JSON_RESPONSE` / `FASTMCP_STATELESS_HTTP` keeps `http_app()` parity
(FIX 2; stateless mode passes `None` for the idle timeout since there are no
sessions to reap); (3) forward `debug` the same way `http_app()` does (FIX C);
(4) extract the whole construction into a module-level
`_build_streamable_http_app()` helper that both `main()` and
`tests/test_session_idle_timeout.py` call, so the manual reproduction can never
drift out of lockstep (FIX 3 - the helper is the SSOT, the smoke test guards
against FastMCP-internals drift).** Configurable via the new
`SESSION_IDLE_TIMEOUT` env (default `3600`,
i.e. 1h ≈ 120× the 30s worst-case tool runtime, so a long in-flight call is never
reaped mid-flight). The PIN TTL of ADR-0029 (24h) is unchanged and unrelated —
this bounds the underlying transport session, not the version/profile pin.

The construction (in `src/mcp/server.py` `main()`):

```python
session_manager = StreamableHTTPSessionManager(
    app=mcp._mcp_server,
    session_idle_timeout=float(os.getenv("SESSION_IDLE_TIMEOUT", "3600")),
)
_app = create_base_app(
    routes=[
        Route("/mcp", endpoint=StreamableHTTPASGIApp(session_manager),
              methods=["GET", "POST", "DELETE"]),
        *mcp._get_additional_http_routes(),   # /health, /ready, /metrics
    ],
    middleware=[Middleware(AuthMiddleware)],
    lifespan=_mcp_session_lifespan,           # runs session_manager.run()
)
```

`_mcp_session_lifespan` becomes the inner lifespan; the existing
`_lifespan_with_pg` (degraded-mode PG retry) wraps it unchanged via
`_app.router.lifespan_context`, preserving the compose order (PG outer, session
inner). The later `/install` StaticFiles mount and the feedback/deploy-key
sub-app mount are unaffected. `mcp._get_additional_http_routes()` is spliced in
explicitly because `http_app()` used to fold the `@mcp.custom_route` endpoints
(`/health`, `/ready`, `/metrics`) in for us.

### Why not the alternatives

- **Do nothing / keep deferred.** Upstream PR #3443 has no ETA (`DON'T MERGE`,
  dirty). The leak grows with uptime. We chose to bound it now rather than wait
  on an indefinitely-blocked upstream PR.
- **Bump FastMCP to v3.** High risk: v3 changes `@mcp.tool()` decorator behaviour
  (the `functools.wraps` trick + `READONLY_TOOL_KWARGS`) and would need a full
  25-tool regression — AND v3.4.2 STILL omits the kwarg, so it does not even fix
  the problem until PR #3443 merges.
- **Monkey-patch the session manager after `http_app()` returns.** Even more
  fragile (depends on both Starlette route internals AND the session manager
  being mutable post-construction; the idle scope is created per session at run
  time). Not cleaner than Option B.

## Consequences

**Positive.** Abandoned sessions are reaped after `SESSION_IDLE_TIMEOUT`,
bounding the leak. Contained blast radius (one block in `main()`); no tool change
(tool count stays 25), no Postgres migration, no client change.

**Negative (accepted).** The construction depends on FastMCP *private* internals
(`mcp._mcp_server`, `mcp._lifespan_manager()`, `mcp._get_additional_http_routes()`)
plus the public-but-undocumented `StreamableHTTPASGIApp` / `create_base_app`. If
FastMCP renames or restructures these, the server fails to start.

**Mitigation.** `tests/test_session_idle_timeout.py` reproduces the construction
at the unit level and asserts the load-bearing invariants (session manager carries
the idle timeout; `/mcp` mounted; `/health`/`/ready`/`/metrics` preserved;
lifespan installed). It breaks early — at CI, not in production — if any internal
drifts. The `main()` block carries a `FRAGILE` comment pointing here.

## Revert triggers

Revert to the upstream `http_app(session_idle_timeout=...)` kwarg (deleting this
bypass) when ANY of the following holds:

1. **FastMCP forwards the kwarg in our pin range (`>=2.3,<3.0`).** PR #3443 (or a
   successor) merges and ships in a 2.x release that `http_app()` /
   `create_streamable_http_app()` actually forward. Then the bypass becomes
   `mcp.http_app(..., session_idle_timeout=...)`.
2. **We bump to FastMCP v3 for other reasons** (provider/transport arch) AND v3
   forwards the kwarg. Wire it through `http_app()` during that migration (which
   already requires the full 25-tool regression).
3. **The smoke test breaks because a FastMCP internal moved.** If `_mcp_server` /
   `_lifespan_manager` / `_get_additional_http_routes` / `StreamableHTTPASGIApp` /
   `create_base_app` is renamed or removed, do NOT chase the new private name —
   re-evaluate against whatever public kwarg/forwarding the new FastMCP version
   offers, and prefer the public path.

## Addendum (2026-06-18, #324) — bumped to FastMCP v3, bypass continues

We bumped `fastmcp>=2.3,<3.0` -> `>=3.2,<4.0` (resolved 3.4.2) to close 3
Dependabot alerts. Revert trigger #2 (bump to v3) fired — but its precondition
(*v3 forwards the kwarg*) did NOT hold: on fastmcp 3.4.2 `http_app()` /
`create_streamable_http_app()` still carry no `session_idle_timeout` parameter,
so **Option B (manual factory) continues** unchanged in shape.

Two v3 API changes touched the factory body (NOT the architecture):

1. **`mcp._deprecated_settings` was removed.** v3's `http_app()` reads
   `json_response` / `stateless_http` / `debug` from the module-level
   `fastmcp.settings` singleton (which also parses `FASTMCP_JSON_RESPONSE` /
   `FASTMCP_STATELESS_HTTP`). `_build_streamable_http_app()` now reads from
   `fastmcp.settings` to preserve FIX 2 / FIX C behaviour byte-for-byte.
2. **All load-bearing internals survived** — `_mcp_server`, `_lifespan_manager`,
   `_get_additional_http_routes`, `StreamableHTTPASGIApp`, `create_base_app`, and
   `StreamableHTTPSessionManager(session_idle_timeout=...)` are all still present
   and accept the same kwargs (verified by spike + the canary test).

Revert trigger #2 stays armed: when a future fastmcp `http_app()` *does* forward
`session_idle_timeout`, delete this factory and wire the public kwarg.
