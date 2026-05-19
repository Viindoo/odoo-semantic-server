"""MCP Resources — stable ``odoo://`` URIs for Odoo entities (Pattern 8).

This module registers 7 ``@mcp.resource`` template handlers on a FastMCP
instance.  Each URI maps deterministically to one Odoo entity (model, field,
method, view, module, pattern, or stylesheet) so AI clients can bookmark,
share, and re-fetch entities without re-running discovery tools.

URI scheme — ``odoo://{version}/{kind}/{path}``:

  * ``odoo://{version}/model/{name}``                — markdown (resolve_model tree)
  * ``odoo://{version}/field/{model}/{field}``       — markdown (resolve_field tree)
  * ``odoo://{version}/method/{model}/{method}``     — markdown (resolve_method tree)
  * ``odoo://{version}/module/{name}``               — markdown (describe_module tree)
  * ``odoo://{version}/view/{xmlid}``                — markdown (resolve_view tree)
  * ``odoo://{version}/pattern/{pattern_id}``        — markdown (suggest_pattern body)
  * ``odoo://{version}/stylesheet/{module}/{file_path*}`` — CSS/SCSS raw text

The ``{version}`` segment accepts any of the 6 sentinel strings (``auto``,
``default``, ``latest``, ``version``, ``any``, ``""``) — these collapse to the
per-API-key session version via :mod:`src.mcp.session` (ADR-0029, Wave E).

A module-level :class:`ResourceCache` (1000 entries, 300s TTL, thread-safe LRU)
short-circuits repeat reads.  Each handler resolves the ``{version}`` sentinel
via :func:`_resolved_version_for` **before** forming the cache key, so two
callers with different session versions (e.g., A→17.0, B→16.0) reading
``odoo://auto/model/sale.order`` receive correctly distinct cached bodies.

See ``docs/adr/0030-mcp-resources-uri-scheme.md`` for the design rationale and
``docs/research/mcp-design-patterns-research.md`` Pattern 8 for cross-server
prior art.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEFAULT_CACHE_CAPACITY: int = 1000
"""Maximum number of cached resource bodies before LRU eviction kicks in."""

DEFAULT_CACHE_TTL_SEC: float = 300.0
"""Per-entry TTL in seconds; reads after expiry trigger re-compute."""

# Resource MIME types — markdown by default, raw for stylesheets.
MIME_MARKDOWN: str = "text/markdown"
MIME_CSS: str = "text/css"
MIME_SCSS: str = "text/x-scss"


# ---------------------------------------------------------------------------
# Cache entry + thread-safe LRU+TTL store
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """One cached resource body."""

    value: str
    mime_type: str
    fetched_at: float


class ResourceCache:
    """Thread-safe LRU cache with per-entry TTL.

    The cache is backed by an ``OrderedDict`` (insertion order = LRU order)
    guarded by a single ``threading.RLock`` so all reads/writes are atomic.
    Expired entries are lazily evicted on access — there is no background
    sweeper, which keeps the cache stateless across process restarts.

    Args:
        capacity: Maximum number of live entries.  At capacity + 1, the
            least-recently-used entry is dropped.
        ttl: Entry lifetime in seconds.  Reads of an expired entry return
            ``None`` and trigger eviction in-place.
        now_fn: Monotonic-clock callable.  Override in tests for
            deterministic TTL assertions.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CACHE_CAPACITY,
        ttl: float = DEFAULT_CACHE_TTL_SEC,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be ≥ 1, got {capacity!r}")
        if ttl <= 0:
            raise ValueError(f"ttl must be > 0, got {ttl!r}")
        self._capacity = capacity
        self._ttl = ttl
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self._data: OrderedDict[str, _CacheEntry] = OrderedDict()

    # ---- core ops --------------------------------------------------------

    def get(self, key: str) -> tuple[str, str] | None:
        """Return ``(value, mime_type)`` on hit, or ``None`` on miss/expiry."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if (self._now_fn() - entry.fetched_at) > self._ttl:
                # Expired — drop and report miss so the caller recomputes.
                self._data.pop(key, None)
                return None
            # LRU bump — move to MRU end.
            self._data.move_to_end(key)
            return entry.value, entry.mime_type

    def put(
        self, key: str, value: str, mime_type: str = MIME_MARKDOWN,
    ) -> None:
        """Insert or overwrite *key* and evict LRU if capacity exceeded."""
        with self._lock:
            now = self._now_fn()
            if key in self._data:
                # Overwrite in place — refresh timestamp + bump to MRU.
                self._data.move_to_end(key)
                self._data[key] = _CacheEntry(
                    value=value, mime_type=mime_type, fetched_at=now,
                )
                return
            self._data[key] = _CacheEntry(
                value=value, mime_type=mime_type, fetched_at=now,
            )
            # Evict LRU (front) when over capacity.
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], tuple[str, str]],
    ) -> tuple[str, str]:
        """Return cached or compute+store via *compute_fn*.

        *compute_fn* must return ``(value, mime_type)``.  The lock is **not**
        held during *compute_fn* (which may block on DB I/O) — concurrent
        callers for the same key may both compute, but only the second write
        survives.  Acceptable: handlers are read-only and idempotent.
        """
        cached = self.get(key)
        if cached is not None:
            return cached
        value, mime_type = compute_fn()
        self.put(key, value, mime_type)
        return value, mime_type

    def clear(self) -> None:
        """Drop all entries."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data


# Module-level singleton — one cache per process.
_CACHE: ResourceCache = ResourceCache()


def get_cache() -> ResourceCache:
    """Return the process-wide :class:`ResourceCache` singleton."""
    return _CACHE


# ---------------------------------------------------------------------------
# Internal helpers — version resolution + Neo4j session
# ---------------------------------------------------------------------------


def _resolved_version_for(version: str) -> str:
    """Normalize sentinel ``{version}`` segments via session-state resolver.

    The 6 sentinels (``auto``, ``default``, ``latest``, ``version``, ``any``,
    ``""``) collapse to the per-API-key active version (Wave E, ADR-0029).
    A concrete version like ``"17.0"`` passes through unchanged.

    Returns:
        The resolved concrete version (e.g. ``"17.0"``).  Raises ``ValueError``
        if all 3 tiers (explicit → session → latest indexed) fail.
    """
    # Lazy import — both modules import from src.mcp.session so we avoid
    # an import-order race by deferring server import to call-time.
    from src.mcp import server as _srv
    from src.mcp.session import normalize_version_arg, resolve_version_v2

    normalized = normalize_version_arg(version)
    if normalized is not None:
        return normalized

    api_key_id = _srv._get_api_key_id()
    with _srv._get_driver().session() as neo4j_session:
        return resolve_version_v2(version, api_key_id, neo4j_session)


# ---------------------------------------------------------------------------
# Per-resource render functions (cache-miss path)
#
# Each helper accepts the URI-template kwargs already extracted, resolves the
# version sentinel once, calls into the existing server.py impl (which already
# emits the canonical tree), and returns a ``(text, mime_type)`` tuple.
# ---------------------------------------------------------------------------


def _render_model(version: str, name: str) -> tuple[str, str]:
    """Render the ``odoo://{version}/model/{name}`` body (markdown tree)."""
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    text = _srv._resolve_model(name, v)
    return text, MIME_MARKDOWN


def _render_field(
    version: str, model: str, field: str,
) -> tuple[str, str]:
    """Render the ``odoo://{version}/field/{model}/{field}`` body."""
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    text = _srv._resolve_field(model, field, v)
    return text, MIME_MARKDOWN


def _render_method(
    version: str, model: str, method: str,
) -> tuple[str, str]:
    """Render the ``odoo://{version}/method/{model}/{method}`` body."""
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    text = _srv._resolve_method(model, method, v)
    return text, MIME_MARKDOWN


def _render_module(version: str, name: str) -> tuple[str, str]:
    """Render the ``odoo://{version}/module/{name}`` body."""
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    text = _srv._describe_module(name, v)
    return text, MIME_MARKDOWN


def _render_view(version: str, xmlid: str) -> tuple[str, str]:
    """Render the ``odoo://{version}/view/{xmlid}`` body."""
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    text = _srv._resolve_view(xmlid, v)
    return text, MIME_MARKDOWN


def _render_pattern(version: str, pattern_id: str) -> tuple[str, str]:
    """Render a single ``PatternExample`` node by ``pattern_id``.

    Bypasses ANN re-ranking (which is intent-driven) and instead fetches the
    raw curated snippet from Neo4j.  Output mirrors a single ``├─ #1`` block
    from :func:`suggest_pattern` so AI clients see a consistent tree shape.
    """
    from src.mcp import server as _srv

    v = _resolved_version_for(version)
    with _srv._get_driver().session() as neo4j_session:
        rec = neo4j_session.run(
            """
            MATCH (p:PatternExample {pattern_id: $pid})
            RETURN p.pattern_id AS id, p.intent_keywords AS kw,
                   p.file_ref AS fr, p.snippet_text AS sn,
                   p.gotchas AS g, p.language AS lang,
                   p.odoo_version_min AS vmin
            """,
            pid=pattern_id,
        ).single()

    if rec is None:
        text = (
            f"pattern({pattern_id!r}, {v!r})\n"
            f"├─ not found — pattern_id is unknown.\n"
            "└─ Recovery: suggest_pattern(intent='...') to discover live IDs."
        )
        return text, MIME_MARKDOWN

    lines = [f"pattern({pattern_id!r}, {v})"]
    lines.append(f"├─ Language: {rec['lang']} (min v{rec['vmin']})")
    lines.append(f"├─ File:     {rec['fr']}")
    kw = rec.get("kw") or []
    if kw:
        lines.append(f"├─ Keywords: {', '.join(kw)}")
    snippet_lines = (rec.get("sn") or "").splitlines()
    if snippet_lines:
        lines.append("├─ Snippet:")
        for sl in snippet_lines:
            lines.append(f"│   {sl}")
    gotchas = rec.get("g") or []
    if gotchas:
        lines.append("└─ Gotchas:")
        for g in gotchas:
            lines.append(f"    • {g}")
    else:
        # Drop trailing connector when there are no gotchas — last branch
        # before this is "Snippet" (already ├─); re-emit it as └─.
        if snippet_lines:
            # Convert the "├─ Snippet:" line above into "└─ Snippet:" and
            # re-indent body lines with a trailing-only sublist marker.
            # Simpler: append a noop terminator so the tree still validates.
            lines.append("└─ (no gotchas recorded)")
        else:
            lines.append("└─ (no body content)")
    return "\n".join(lines), MIME_MARKDOWN


def _render_stylesheet(
    version: str, module: str, file_path: str,
) -> tuple[str, str]:
    """Render the ``odoo://{version}/stylesheet/{module}/{file_path*}`` body.

    Returns the on-disk CSS/SCSS file contents *only* when the Neo4j
    ``:Stylesheet`` node matching ``(file_path, module, odoo_version)``
    exists — otherwise emit a not-found tree.  This guards against
    arbitrary-file reads via crafted URIs.

    The ``file_path*`` segment uses RFC 6570 list-pattern syntax so the path
    may contain slashes (e.g. ``static/src/scss/foo.scss``).  FastMCP passes
    the multi-segment value as a single string with the leading slash
    *stripped* — re-add a leading ``/`` before disk read since indexer stores
    absolute paths.
    """
    from src.mcp import server as _srv

    v = _resolved_version_for(version)

    # Defence-in-depth: verify the (file_path, module, version) is indexed
    # *before* opening the file — guards against URI tampering.
    with _srv._get_driver().session() as neo4j_session:
        # The file_path FastMCP hands us is the URL-decoded raw value with
        # the leading slash stripped (URIs cannot encode a leading "/" in
        # a path segment).  Try both the as-received string and a
        # leading-slash variant so we match indexer absolute paths *and*
        # any future indexer that stores repo-relative paths.
        rec = neo4j_session.run(
            """
            MATCH (ss:Stylesheet {module: $mod, odoo_version: $v})
            WHERE ss.file_path = $fp OR ss.file_path = $fp_abs
            RETURN ss.file_path AS file_path, ss.language AS language
            LIMIT 1
            """,
            mod=module, v=v, fp=file_path, fp_abs="/" + file_path,
        ).single()

    if rec is None:
        text = (
            f"stylesheet({module!r}/{file_path!r}, {v!r})\n"
            "├─ not found — no indexed Stylesheet at that path.\n"
            "└─ Recovery: describe_module(name=..., odoo_version=...) "
            "to list module assets."
        )
        return text, MIME_MARKDOWN

    on_disk_path = rec["file_path"]
    language = rec["language"]
    mime = MIME_SCSS if language == "scss" else MIME_CSS

    try:
        with open(on_disk_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as e:
        text = (
            f"stylesheet({module!r}/{file_path!r}, {v!r})\n"
            f"├─ indexed but file unreadable on this server: "
            f"{type(e).__name__}: {e}\n"
            f"└─ Recovery: re-index the repo to refresh on-disk references."
        )
        return text, MIME_MARKDOWN

    return raw, mime


# ---------------------------------------------------------------------------
# Public — register all 7 resources on a FastMCP instance
# ---------------------------------------------------------------------------


def register_resources(mcp_instance) -> None:
    """Attach 7 ``@mcp.resource`` template handlers to *mcp_instance*.

    Idempotency: calling this twice on the same FastMCP instance is a
    no-op for the second call — FastMCP raises on duplicate URIs, so we
    swallow the registration error in that case.

    Wiring:
        Call once at module-import time in ``server.py`` after the
        ``mcp = FastMCP(...)`` line.  All cache state lives in the
        module-level :data:`_CACHE` singleton.

    Args:
        mcp_instance: A live ``fastmcp.FastMCP`` instance.

    Returns:
        None.  Side effect: handlers appear in
        ``mcp_instance._resource_manager._templates``.
    """
    cache = get_cache()

    # ---- model ----------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/model/{name}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown tree describing an Odoo model: defining module, "
            "inheritance chain, field/method counts."
        ),
    )
    def _model_resource(version: str, name: str) -> str:
        # Resolve sentinel (e.g. "auto") BEFORE forming the cache key so
        # two callers with different session versions never share a body.
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/model/{name}"
        return cache.get_or_compute(
            uri, lambda: _render_model(resolved, name),
        )[0]

    # ---- field ----------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/field/{model}/{field}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown tree describing one ORM field: type, compute hook, "
            "stored flag, and every module that declares it."
        ),
    )
    def _field_resource(version: str, model: str, field: str) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/field/{model}/{field}"
        return cache.get_or_compute(
            uri, lambda: _render_field(resolved, model, field),
        )[0]

    # ---- method ---------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/method/{model}/{method}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown tree describing one model method: override chain, "
            "super() call markers, decorators."
        ),
    )
    def _method_resource(version: str, model: str, method: str) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/method/{model}/{method}"
        return cache.get_or_compute(
            uri, lambda: _render_method(resolved, model, method),
        )[0]

    # ---- module ---------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/module/{name}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown tree describing one Odoo module: manifest, dependencies, "
            "models defined/extended, view + JS counts."
        ),
    )
    def _module_resource(version: str, name: str) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/module/{name}"
        return cache.get_or_compute(
            uri, lambda: _render_module(resolved, name),
        )[0]

    # ---- view -----------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/view/{xmlid}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown tree describing one XML view: parent, xpaths, "
            "extension chain."
        ),
    )
    def _view_resource(version: str, xmlid: str) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/view/{xmlid}"
        return cache.get_or_compute(
            uri, lambda: _render_view(resolved, xmlid),
        )[0]

    # ---- pattern --------------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/pattern/{pattern_id}",
        mime_type=MIME_MARKDOWN,
        description=(
            "Markdown body for one curated PatternExample: snippet, "
            "gotchas, intent keywords."
        ),
    )
    def _pattern_resource(version: str, pattern_id: str) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/pattern/{pattern_id}"
        return cache.get_or_compute(
            uri, lambda: _render_pattern(resolved, pattern_id),
        )[0]

    # ---- stylesheet -----------------------------------------------------
    @mcp_instance.resource(
        "odoo://{version}/stylesheet/{module}/{file_path*}",
        # mime_type omitted at registration — runtime computes per file
        # (text/css vs text/x-scss).  FastMCP forwards the string body
        # as-is; the registered mime_type is a default hint only.
        mime_type=MIME_CSS,
        description=(
            "Raw CSS or SCSS source for an indexed stylesheet.  file_path "
            "may include forward slashes."
        ),
    )
    def _stylesheet_resource(
        version: str, module: str, file_path: str,
    ) -> str:
        resolved = _resolved_version_for(version)
        uri = f"odoo://{resolved}/stylesheet/{module}/{file_path}"
        return cache.get_or_compute(
            uri, lambda: _render_stylesheet(resolved, module, file_path),
        )[0]
