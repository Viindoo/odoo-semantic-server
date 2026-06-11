"""Smoke guard for Option B session_idle_timeout wiring (#279 item 1, ADR-0049).

The MCP server's main() block bypasses FastMCP's ``mcp.http_app()`` and builds the
streamable-http app directly so it can pass ``session_idle_timeout`` to the MCP
SDK's ``StreamableHTTPSessionManager`` — FastMCP (<3.0, pinned) does NOT forward
that kwarg, so an abandoned streamable-http session would otherwise leak until
process restart.

That construction depends on FastMCP/MCP-SDK *private* internals
(``mcp._mcp_server``, ``mcp._lifespan_manager()``,
``mcp._get_additional_http_routes()``) plus the public-but-undocumented
``StreamableHTTPASGIApp`` / ``create_base_app``. main() and this test BOTH call
the single ``srv._build_streamable_http_app`` helper (no duplicated construction
to drift), and this test asserts the load-bearing invariants:

  * the session manager actually carries the configured ``session_idle_timeout``
    (the whole point of Option B — a regression here silently re-opens the leak);
  * the app mounts ``/mcp`` AND preserves the ``@mcp.custom_route`` endpoints
    (/health, /ready, /metrics) that ``http_app()`` used to include for us —
    dropping them would break liveness/readiness probes;
  * the lifespan that drives ``session_manager.run()`` is installed on the app
    router (so ``_lifespan_with_pg`` can wrap it).

If FastMCP renames or removes any of these internals the imports/attributes here
break — that is the intended early-warning signal to revert to the upstream
``http_app(session_idle_timeout=...)`` kwarg per ADR-0049.

Pure construction-level — no Docker, no Neo4j, no Postgres, no running server.
"""

import os

import src.mcp.server as srv


def _build_app(idle_timeout: float, *, stateless: bool = False):
    """Build the Option B app via the SAME helper main() uses (no duplication).

    Calling ``srv._build_streamable_http_app`` directly is what keeps this test
    in lockstep with main(): there is exactly one construction site, so the
    asserts below verify the invariants against the live FastMCP/MCP-SDK without
    a hand-mirrored copy that could silently drift.

    ``stateless`` is forced on ``mcp._deprecated_settings.stateless_http`` for the
    duration of the build (restored after) so CI running with
    ``FASTMCP_STATELESS_HTTP=true`` set in the environment cannot flip a
    session-mode test into a false pass/fail — the idle-timeout invariants below
    only hold in session mode (stateless passes ``None``, asserted separately).
    """
    settings = srv.mcp._deprecated_settings
    saved = settings.stateless_http
    settings.stateless_http = stateless
    try:
        return srv._build_streamable_http_app(
            idle_timeout=idle_timeout,
            middleware=[],
        )
    finally:
        settings.stateless_http = saved


def test_session_manager_carries_idle_timeout():
    """The session manager is built with the configured idle timeout (Option B)."""
    _app, session_manager = _build_app(1234.0)
    assert session_manager.session_idle_timeout == 1234.0, (
        "StreamableHTTPSessionManager must carry session_idle_timeout — without "
        "it abandoned sessions leak until process restart (#279)"
    )


def test_stateless_mode_passes_none_idle_timeout():
    """In stateless mode the manager gets session_idle_timeout=None (FIX D).

    The MCP SDK rejects a numeric session_idle_timeout when ``stateless=True``
    (there are no sessions to reap — passing a number raises RuntimeError). The
    Option B build must therefore degrade the configured timeout to ``None`` when
    an operator opts into FASTMCP_STATELESS_HTTP, not crash the boot.
    """
    _app, session_manager = _build_app(1234.0, stateless=True)
    assert session_manager.session_idle_timeout is None, (
        "stateless mode must pass session_idle_timeout=None — a numeric value is "
        "rejected by the SDK and would crash startup"
    )


def test_default_idle_timeout_is_one_hour():
    """main()'s default (env unset) resolves to 3600s — matches .env.example."""
    saved = os.environ.pop("SESSION_IDLE_TIMEOUT", None)
    try:
        assert srv._resolve_session_idle_timeout() == 3600.0
    finally:
        if saved is not None:
            os.environ["SESSION_IDLE_TIMEOUT"] = saved


def test_invalid_idle_timeout_falls_back_to_default():
    """A non-numeric SESSION_IDLE_TIMEOUT must NOT crash startup (FIX 1).

    A raw float("abc") would raise ValueError and abort the server boot; the
    value guard falls back to the 3600s default instead.
    """
    saved = os.environ.get("SESSION_IDLE_TIMEOUT")
    os.environ["SESSION_IDLE_TIMEOUT"] = "abc"
    try:
        assert srv._resolve_session_idle_timeout() == 3600.0
    finally:
        if saved is None:
            os.environ.pop("SESSION_IDLE_TIMEOUT", None)
        else:
            os.environ["SESSION_IDLE_TIMEOUT"] = saved


def test_nonpositive_idle_timeout_clamps_to_default():
    """A <= 0 SESSION_IDLE_TIMEOUT clamps to the default (FIX 1).

    The MCP SDK rejects session_idle_timeout <= 0 with ValueError (it has no
    "0 = disable" affordance — disable is None, which Option B never passes).
    Clamping keeps reaping ON rather than crashing or silently re-opening the
    #279 leak.
    """
    saved = os.environ.get("SESSION_IDLE_TIMEOUT")
    try:
        for bad in ("0", "-1", "-30.5"):
            os.environ["SESSION_IDLE_TIMEOUT"] = bad
            assert srv._resolve_session_idle_timeout() == 3600.0, (
                f"SESSION_IDLE_TIMEOUT={bad} must clamp to 3600, not pass through"
            )
    finally:
        if saved is None:
            os.environ.pop("SESSION_IDLE_TIMEOUT", None)
        else:
            os.environ["SESSION_IDLE_TIMEOUT"] = saved


def test_nonfinite_idle_timeout_clamps_to_default():
    """A non-finite SESSION_IDLE_TIMEOUT (nan/inf) clamps to the default (FIX A).

    ``float("nan")``/``float("inf")`` parse without raising, and ``nan <= 0`` is
    ``False`` — a bare ``<= 0`` guard would let them through. ``nan`` produces a
    deadline that never compares true and ``inf`` one that never expires: either
    silently disables reaping and re-opens the #279 leak. Both must clamp to 3600.
    """
    saved = os.environ.get("SESSION_IDLE_TIMEOUT")
    try:
        for bad in ("nan", "NaN", "inf", "Infinity", "-inf"):
            os.environ["SESSION_IDLE_TIMEOUT"] = bad
            assert srv._resolve_session_idle_timeout() == 3600.0, (
                f"SESSION_IDLE_TIMEOUT={bad} must clamp to 3600, not pass a "
                "non-finite deadline through to the SDK"
            )
    finally:
        if saved is None:
            os.environ.pop("SESSION_IDLE_TIMEOUT", None)
        else:
            os.environ["SESSION_IDLE_TIMEOUT"] = saved


def test_app_mounts_mcp_and_preserves_custom_routes():
    """/mcp is mounted AND the @mcp.custom_route probes survive Option B.

    http_app() used to fold the custom routes in for us; the manual build must
    splice mcp._get_additional_http_routes() back in or /health, /ready, /metrics
    silently disappear.
    """
    app, _ = _build_app(3600.0)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp" in paths, "Option B must mount the /mcp streamable-http endpoint"
    for probe in ("/health", "/ready", "/metrics"):
        assert probe in paths, (
            f"custom route {probe} dropped — Option B must preserve "
            "mcp._get_additional_http_routes() (liveness/readiness/metrics)"
        )


def test_mcp_route_allows_get_post_delete():
    """The /mcp route accepts the 3 methods streamable-http needs (incl. DELETE,
    the explicit session-close verb)."""
    app, _ = _build_app(3600.0)
    mcp_route = next(r for r in app.routes if getattr(r, "path", None) == "/mcp")
    assert {"GET", "POST", "DELETE"} <= set(mcp_route.methods)


def test_session_lifespan_installed_on_router():
    """create_base_app installs our session lifespan on the router so
    _lifespan_with_pg can wrap it (compose order: PG outer, session inner)."""
    app, _ = _build_app(3600.0)
    assert app.router.lifespan_context is not None, (
        "the session-manager lifespan must be installed so _lifespan_with_pg "
        "can wrap it"
    )
