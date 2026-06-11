# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anti-freeze guards for the MCP query-embed hot path (issue #227 + #276 G7).

Business rules under test (mechanism, not output):
  1. The query-embed tools (find_examples / suggest_pattern / find_style_override)
     embed OFF the event loop WITHOUT blocking it — a slow embed must not stall a
     concurrent cheap coroutine (proxy for /health staying responsive).
  2. A burst of concurrent embeds beyond EMBEDDER_MAX_CONCURRENCY fails fast
     (EmbedOverloaded) rather than queueing unbounded.
  3. A giant query is truncated to the token budget BEFORE it is embedded.
  4. The /ready route is wired and returns 200 with cache metadata.
  5. The three hot-path tools are async coroutine functions; the actual embed
     runs in a worker thread holding a thread-bound BoundedSemaphore slot so a
     client-cancel cannot free the slot mid-embed (#276 G7).

#276 G7 note: the embed worker thread now calls the SYNC embed path DIRECTLY
(``_embed_with_timeout`` → query client, or ``embed`` fallback) rather than
``embed_async`` — calling ``embed_async`` inside the already-offloaded thread
would nest a second to_thread / child loop. These tests therefore record the
embed on the SYNC ``embed`` method (the path the hot loop now exercises) and
assert the EVENT LOOP stays responsive, not which embed method name was used.

No DB containers required — these are pure-unit tests with the embedder stubbed.
"""
import asyncio
import inspect
import threading
import time

import pytest

from src.constants import EMBEDDER_MAX_CONCURRENCY, EMBEDDER_TOKEN_BUDGET
from src.mcp import server as srv

# ---------------------------------------------------------------------------
# Stub embedders
# ---------------------------------------------------------------------------


class _SlowAsyncEmbedder:
    """Records SYNC embed calls; sleeps to simulate a slow upstream embed.

    #276 G7: the hot path now calls the SYNC embed path inside a worker thread
    (NOT embed_async). This stub deliberately does NOT define _embed_with_timeout
    so _embed_sync_query exercises its ``embed`` fallback, and the delay is a
    blocking sleep in ``embed`` so the slot is genuinely held for the duration
    (mirroring a slow Ollama round-trip). ``embed_async`` is kept for Protocol
    parity but must NOT be hit on the hot path.
    """

    model = "stub"
    chars_per_token = 4.0

    def __init__(self, delay: float = 0.2):
        self.delay = delay
        self.async_calls = 0
        self.sync_calls = 0
        self.embedded_texts: list[str] = []
        self._lock = threading.Lock()

    def embed(self, texts):
        with self._lock:
            self.sync_calls += 1
            self.embedded_texts.extend(texts)
        if self.delay:
            time.sleep(self.delay)  # blocking — holds the thread-bound slot
        return [[0.1] * 8 for _ in texts]

    async def embed_async(self, texts, *, read_timeout=None):  # pragma: no cover
        # Protocol parity only — the #276 G7 hot path must not call this.
        self.async_calls += 1
        return self.embed(texts)


@pytest.fixture(autouse=True)
def _reset_embed_semaphore():
    """Each test gets a fresh thread-bound embed semaphore.

    The embed pool is now a _LazyBoundedSemaphore object (#279 consolidation);
    .reset() drops the built semaphore so the next .get() rebuilds from the
    current env (e.g. a monkeypatched EMBEDDER_SLOT_ACQUIRE_TIMEOUT) — same intent
    as the previous "null the module globals" reset, against the new structure.
    """
    srv._embed_pool.reset()
    yield
    srv._embed_pool.reset()


# ---------------------------------------------------------------------------
# 1. Slow embed does not block the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_embed_does_not_block_event_loop():
    """While one _embed_query is sleeping, a separate coroutine still runs.

    If the embed blocked the loop (the #227 bug), the cheap coroutine could not
    make progress until the embed returned. We assert the cheap coroutine
    completes well before the slow embed.
    """
    embedder = _SlowAsyncEmbedder(delay=0.3)
    progressed = []

    async def _cheap_heartbeat():
        # Proxy for /health: must tick repeatedly while the embed is in flight.
        for _ in range(3):
            await asyncio.sleep(0.01)
            progressed.append(time.monotonic())

    embed_task = asyncio.create_task(srv._embed_query(embedder, "INSTRUCT:", "q"))
    start = time.monotonic()
    await _cheap_heartbeat()
    heartbeat_done = time.monotonic()
    await embed_task

    # Heartbeat finished its 3 ticks (~0.03s) long before the 0.3s embed.
    # This is the LOAD-BEARING assertion: the event loop stayed responsive while
    # the embed ran in a worker thread (#227 + #276 G7).
    assert heartbeat_done - start < 0.2, (
        "cheap coroutine was blocked behind the slow embed — event loop froze"
    )
    # The embed ran exactly once, off the loop, via the SYNC embed path (#276 G7
    # runs sync embed directly in the worker thread, never embed_async).
    assert embedder.sync_calls == 1, "the query must be embedded exactly once"
    assert embedder.async_calls == 0, (
        "hot path must call the SYNC embed in the worker thread, not embed_async"
        " (avoids a nested to_thread / child loop — #276 G7 R-A6)"
    )


# ---------------------------------------------------------------------------
# 2. Semaphore fails fast on overload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_semaphore_fails_fast_when_saturated(monkeypatch):
    """Once EMBEDDER_MAX_CONCURRENCY slots are held, the next embed rejects fast.

    We shrink the acquire timeout (via the env knob the thread-bound semaphore
    reads at build time, #276 G7) and hold all slots with long-running embeds,
    then assert the overflow caller raises EmbedOverloaded instead of waiting for
    a slot.
    """
    # The autouse fixture reset the semaphore to None, so it rebuilds from this
    # env on first use, picking up the short acquire timeout.
    monkeypatch.setenv("EMBEDDER_SLOT_ACQUIRE_TIMEOUT", "0.05")
    embedder = _SlowAsyncEmbedder(delay=5.0)  # holds its slot for the whole test

    # Saturate every slot.
    holders = [
        asyncio.create_task(srv._embed_query(embedder, "I:", f"q{i}"))
        for i in range(EMBEDDER_MAX_CONCURRENCY)
    ]
    # Barrier: wait until every holder has acquired its thread-bound slot and
    # entered embed. ``sync_calls`` increments INSIDE embed, which runs only
    # AFTER the slot is acquired in _embed_query_in_thread — so sync_calls == N
    # proves all N slots are held. Deterministic, unlike a fixed sleep that
    # races thread-pool startup latency on a loaded host.
    deadline = time.monotonic() + 5.0
    while embedder.sync_calls < EMBEDDER_MAX_CONCURRENCY:
        if time.monotonic() > deadline:  # pragma: no cover - CI watchdog
            raise AssertionError(
                "holders did not saturate the embed pool: "
                f"{embedder.sync_calls}/{EMBEDDER_MAX_CONCURRENCY} slots acquired"
            )
        await asyncio.sleep(0.01)

    start = time.monotonic()
    with pytest.raises(srv.EmbedOverloaded):
        await srv._embed_query(embedder, "I:", "overflow")
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, "overflow embed must fail fast, not block on the queue"

    for h in holders:
        h.cancel()
    await asyncio.gather(*holders, return_exceptions=True)


# ---------------------------------------------------------------------------
# 3. Giant query is capped before embedding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_giant_query_is_capped_before_embed():
    """A kilobyte query is truncated to the token budget before it is embedded."""
    embedder = _SlowAsyncEmbedder(delay=0.0)
    huge = "x" * 1_000_000  # way over the budget
    await srv._embed_query(embedder, "INSTRUCT:", huge)

    assert len(embedder.embedded_texts) == 1
    sent = embedder.embedded_texts[0]
    # Sent text = instruct + capped query. The capped query must be <= the
    # char budget derived from EMBEDDER_TOKEN_BUDGET * chars_per_token.
    char_budget = int(EMBEDDER_TOKEN_BUDGET * embedder.chars_per_token)
    capped_query_len = len(sent) - len("INSTRUCT:")
    assert capped_query_len <= char_budget, (
        f"query not capped: {capped_query_len} chars > budget {char_budget}"
    )
    assert capped_query_len < len(huge), "huge query was not truncated at all"


def test_cap_query_text_noop_for_short_query():
    """A short query passes through _cap_query_text unchanged (cheap no-op)."""
    embedder = _SlowAsyncEmbedder()
    assert srv._cap_query_text(embedder, "small query") == "small query"


# ---------------------------------------------------------------------------
# 4. /ready route is wired and returns cache metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_route_registered_and_returns_200():
    """The /ready custom route is registered and returns 200 + cache fields."""
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.mcp import health as health_mod

    # /ready must be a registered custom HTTP route (NOT an MCP tool). Inspect
    # the FastMCP additional-routes registry directly — building http_app() here
    # has app-wide side effects that leak into sibling resource tests.
    ready_paths = [
        getattr(r, "path", None) for r in srv.mcp._additional_http_routes
    ]
    assert "/ready" in ready_paths, "/ready custom route must be registered"

    req = MagicMock()
    req.url.path = "/ready"
    fake_cache = {
        "embeddings_total": 123,
        "embeddings_by_chunk_type": {"method": 123},
        "cached_at": time.monotonic(),
    }
    with (
        patch.object(health_mod, "_ready_cache", fake_cache),
        patch.object(health_mod, "_check_neo4j", AsyncMock(return_value="ok")),
        patch.object(health_mod, "_check_pg", AsyncMock(return_value="ok")),
    ):
        resp = await srv.ready_check(req)

    assert resp.status_code == 200
    data = json.loads(resp.body)
    assert data["status"] == "ok"
    # Count cache metadata is present (proves the route reads the cache).
    assert data["embeddings_total"] == 123
    assert "cache_ttl_s" in data and "cache_age_s" in data


# ---------------------------------------------------------------------------
# 5. The hot-path tools are async and route through embed_async
# ---------------------------------------------------------------------------


def test_hot_path_tools_are_coroutine_functions():
    """find_examples / suggest_pattern / find_style_override / entity_lookup
    must be async so FastMCP awaits them on the loop instead of blocking it."""
    for name in (
        "find_examples",
        "suggest_pattern",
        "find_style_override",
        "entity_lookup",
    ):
        fn = srv.mcp._tool_manager._tools[name].fn
        assert inspect.iscoroutinefunction(fn), (
            f"{name} must be an async def tool (#227)"
        )


@pytest.mark.asyncio
async def test_find_examples_tool_embeds_offloop_and_passes_vector(monkeypatch):
    """find_examples embeds the query off the loop, then hands the vector to the
    blocking impl so it does not re-embed inside the worker thread.

    Business rule (#227 + #276 G7): the embed happens before the blocking DB body
    and its result is passed down as ``_query_vec``. The embed runs via the SYNC
    embed path in a worker thread (#276 G7), never the unbounded sync-on-loop path.

    The blocking DB body is short-circuited by stubbing _find_examples so this
    stays a pure-unit test (no DB).
    """
    embedder = _SlowAsyncEmbedder(delay=0.0)
    monkeypatch.setattr(srv, "_get_embedder", lambda: embedder)

    captured = {}

    def _fake_impl(*args, **kwargs):
        captured["query_vec"] = kwargs.get("_query_vec")
        return "find_examples: stub\nFound 0 results\n"

    monkeypatch.setattr(srv, "_find_examples", _fake_impl)

    out = await srv.find_examples.fn(query="confirm sale order", odoo_version="17.0")
    assert "find_examples" in out
    # Exactly one embed, via the SYNC path the #276 G7 worker thread uses.
    assert embedder.sync_calls == 1, "tool must embed the query exactly once"
    assert embedder.async_calls == 0, (
        "tool must NOT call embed_async on the hot path (#276 G7 R-A6)"
    )
    # The pre-computed vector is handed to the blocking impl so it does not
    # re-embed inside the worker thread.
    assert captured["query_vec"] is not None


# ---------------------------------------------------------------------------
# 6. (#227 ROOT) Every sync DB tool is now offloaded off the event loop
# ---------------------------------------------------------------------------

# The four hot-path tools that pre-embed on the loop themselves — they are async
# by hand, NOT via @offload, and must stay that way.
_HANDWRITTEN_ASYNC_TOOLS = {
    "find_examples",
    "suggest_pattern",
    "find_style_override",
    "entity_lookup",
}


def test_all_sync_db_tools_are_offloaded_coroutines():
    """Every registered tool runs as a coroutine — none blocks the event loop.

    Business rule (#227 root cause): FastMCP 2.14.x runs a sync `def` tool body
    directly on the event-loop thread, so any Neo4j/PG I/O freezes /health. The
    fix makes EVERY tool an awaitable — the 4 hot-path tools async by hand and
    the remaining 20 DB tools via the @offload decorator. If a future tool is
    added as a plain sync `def`, this guard goes red.
    """
    tools = srv.mcp._tool_manager._tools
    blocking = [
        name for name, t in tools.items()
        if not inspect.iscoroutinefunction(t.fn)
    ]
    assert not blocking, (
        f"these tools still run synchronously on the event loop (#227): {blocking}"
    )


def test_offloaded_tool_preserves_signature_for_schema():
    """@offload must keep the original handler signature so FastMCP's input
    schema stays correct (the decorator uses functools.wraps → __wrapped__).

    Regression guard: a generic *a/**k wrapper without wraps would either crash
    FastMCP ("**kwargs not supported") or erase every parameter from the schema.
    """
    mi = srv.mcp._tool_manager._tools["model_inspect"]
    props = mi.parameters.get("properties", {})
    # model_inspect exposes a real, named parameter set — not an empty/opaque one.
    assert "model" in props, "offload erased the tool's input schema"
    # And it is genuinely a coroutine FastMCP will await.
    assert inspect.iscoroutinefunction(mi.fn)


@pytest.mark.asyncio
async def test_offloaded_sync_tool_does_not_block_event_loop():
    """A slow sync tool body wrapped by @offload runs in a worker thread, so a
    concurrent cheap coroutine (proxy for /health) keeps ticking.

    We wrap a deliberately blocking sync function with the real srv.offload and
    assert the heartbeat finishes long before the blocking body — proving the
    body left the event-loop thread.
    """
    import time as _time

    @srv.offload
    def _slow_blocking_tool(x: str) -> str:
        _time.sleep(0.3)  # blocking sleep — would freeze the loop if run on it
        return f"done:{x}"

    progressed = []

    async def _cheap_heartbeat():
        for _ in range(3):
            await asyncio.sleep(0.01)
            progressed.append(_time.monotonic())

    task = asyncio.create_task(_slow_blocking_tool("q"))
    start = _time.monotonic()
    await _cheap_heartbeat()
    heartbeat_done = _time.monotonic()
    result = await task

    assert result == "done:q"
    assert heartbeat_done - start < 0.2, (
        "heartbeat blocked behind the sync tool body — @offload did not move it "
        "off the event loop"
    )


def test_offload_propagates_contextvar_into_worker_thread():
    """asyncio.to_thread copies the ContextVar context, so the per-request
    api_key_id set on the loop is visible inside the offloaded worker thread.

    This guards the auth/tenant scoping invariant: an offloaded tool must read
    the SAME api_key_id the middleware set for this request, not 'default'.
    """

    @srv.offload
    def _read_api_key() -> str:
        return srv._get_api_key_id()

    async def _run():
        token = srv._api_key_id_var.set("key-abc-123")
        try:
            return await _read_api_key()
        finally:
            srv._api_key_id_var.reset(token)

    assert asyncio.run(_run()) == "key-abc-123"


# ---------------------------------------------------------------------------
# 7. (#2 review) Query instruction is read per-backend, never hardcoded
# ---------------------------------------------------------------------------


class _NoInstructAsyncEmbedder(_SlowAsyncEmbedder):
    """An OpenAI/TEI-style backend: query_instruction is the empty string.

    Such backends embed queries with NO instruction prefix. The server must
    honour this and NOT prepend the Qwen INSTRUCT_NL_TO_CODE prefix, or the
    query lands in the wrong region of the vector space.
    """

    query_instruction = ""


@pytest.mark.asyncio
async def test_query_embed_uses_backend_instruction_no_qwen_prefix(monkeypatch):
    """With a query_instruction='' backend, the embedded query has NO prefix.

    Business rule (#2): callsites must read embedder.query_instruction, not the
    hardcoded Qwen INSTRUCT_NL_TO_CODE. For an OpenAI/TEI backend that means the
    query is embedded verbatim (instruct=''), so the text sent to embed_async
    equals the raw query with no Qwen instruction glued on the front.
    """
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    embedder = _NoInstructAsyncEmbedder(delay=0.0)
    monkeypatch.setattr(srv, "_get_embedder", lambda: embedder)
    monkeypatch.setattr(
        srv, "_find_examples",
        lambda *a, **k: "find_examples: stub\nFound 0 results\n",
    )

    await srv.find_examples.fn(query="confirm sale order", odoo_version="17.0")

    assert embedder.embedded_texts, "query was never embedded"
    sent = embedder.embedded_texts[0]
    assert not sent.startswith(INSTRUCT_NL_TO_CODE), (
        "Qwen instruction prefix leaked onto an OpenAI/TEI query — callsite "
        "hardcoded INSTRUCT_NL_TO_CODE instead of reading query_instruction"
    )
    assert sent == "confirm sale order", (
        "empty-instruction backend must embed the query verbatim"
    )


@pytest.mark.asyncio
async def test_query_embed_keeps_qwen_prefix_when_backend_requires_it(monkeypatch):
    """A backend exposing the Qwen instruction still gets it prepended.

    Counterpart to the no-prefix test: when query_instruction is the Qwen
    prefix, the query must be embedded WITH it (the historical behaviour),
    proving the fix reads the attribute rather than always stripping it.
    """
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    class _QwenAsyncEmbedder(_SlowAsyncEmbedder):
        query_instruction = INSTRUCT_NL_TO_CODE

    embedder = _QwenAsyncEmbedder(delay=0.0)
    monkeypatch.setattr(srv, "_get_embedder", lambda: embedder)
    monkeypatch.setattr(
        srv, "_find_examples",
        lambda *a, **k: "find_examples: stub\nFound 0 results\n",
    )

    await srv.find_examples.fn(query="confirm sale order", odoo_version="17.0")

    sent = embedder.embedded_texts[0]
    assert sent.startswith(INSTRUCT_NL_TO_CODE), (
        "Qwen backend lost its required instruction prefix"
    )


# ---------------------------------------------------------------------------
# M6 — exception-detail must not leak to the agent-facing tool output (CWE-209)
# ---------------------------------------------------------------------------

# Canary string carried inside a raised exception. If it ever appears in a
# tool's returned text, the handler is interpolating exception internals into
# client output again. The exception CLASS name must not leak either.
_LEAK_CANARY = "boom-/srv/internal/secret/trace-canary-RuntimeError-detail"


async def test_suggest_pattern_embedder_failure_does_not_leak_exception(monkeypatch):
    """suggest_pattern degrades cleanly when _get_embedder raises — no internals.

    FAILS if the handler reverts to `f"... {type(e).__name__}: {e}"`: the raised
    message text and the exception class name must both be absent from output.
    """
    def _raise():
        raise RuntimeError(_LEAK_CANARY)

    monkeypatch.setattr(srv, "_get_embedder", _raise)

    out = await srv.suggest_pattern.fn(intent="compute total", odoo_version="17.0")

    assert "embedder unavailable" in out  # still agent-actionable
    assert _LEAK_CANARY not in out
    assert "RuntimeError" not in out


async def test_find_style_override_embedder_failure_does_not_leak_exception(monkeypatch):
    """find_style_override degrades cleanly when _get_embedder raises — no internals."""
    def _raise():
        raise RuntimeError(_LEAK_CANARY)

    monkeypatch.setattr(srv, "_get_embedder", _raise)

    # NL phrase (has spaces) → not a literal token → embedder is the first I/O.
    out = await srv.find_style_override.fn("primary button color variable", "17.0")

    assert "embedder unavailable" in out  # still agent-actionable
    assert _LEAK_CANARY not in out
    assert "RuntimeError" not in out
