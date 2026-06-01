# SPDX-License-Identifier: AGPL-3.0-or-later
"""pgvector writer — chunk, embed, and store Odoo code in PostgreSQL embeddings table."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg2.extras import execute_values

from src.constants import EMBEDDER_TOKEN_BUDGET

from .embedder import EmbedderClient, estimate_tokens, split_by_token_budget
from .models import (
    CSSChunk,
    JSChunk,
    ModuleInfo,
    ParseResult,
    PatternExample,
    SCSSChunk,
    ViewParseResult,
)

_logger = logging.getLogger(__name__)

_WINDOW_CHARS = 2048
_OVERLAP_CHARS = 256

_INSERT_SQL = """
INSERT INTO embeddings
    (chunk_type, module, odoo_version, entity_name, model_name, file_path, chunk_idx, content, vec,
     profile_name, line_start, repo, repo_id, embedding_model, embedding_dim)
VALUES %s
ON CONFLICT ON CONSTRAINT ux_embeddings_chunk
DO UPDATE SET content = EXCLUDED.content, vec = EXCLUDED.vec, indexed_at = NOW(),
              line_start = EXCLUDED.line_start, repo = EXCLUDED.repo, repo_id = EXCLUDED.repo_id,
              embedding_model = EXCLUDED.embedding_model, embedding_dim = EXCLUDED.embedding_dim
"""


@dataclass
class EmbeddingChunk:
    """A single embeddable text unit derived from Odoo source code."""
    chunk_type: str     # 'method'|'field'|'view'|'qweb'|'js_era1'|'js_era2'|'js_era3'|'css'|'scss'|'less'  # noqa: E501
    module: str
    odoo_version: str
    entity_name: str
    model_name: str | None
    file_path: str
    chunk_idx: int
    content: str
    profile_name: str | None = None  # NULL = shared/global (pattern chunks, pre-tenant rows)
    # A3 — provenance columns (reindex-forcing; NULL for css/scss/less/pattern chunks)
    line_start: int | None = None   # 1-based source line of the entity (method def / field assign)
    repo: str | None = None         # repo basename (ModuleInfo.repo)
    repo_id: int | None = None      # FK to repos.id (ModuleInfo.repo_id)

    def as_tuple(
        self, vec: list[float],
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> tuple:
        return (
            self.chunk_type, self.module, self.odoo_version,
            self.entity_name, self.model_name, self.file_path,
            self.chunk_idx, self.content, vec,
            self.profile_name,
            self.line_start, self.repo, self.repo_id,
            embedding_model, embedding_dim,
        )


def _embedder_meta(embedder: object) -> tuple[str | None, int | None]:
    """Return (model, dim) from an embedder object; None for missing attrs.

    Tolerates pre-ADR-0045 embedders and test doubles that do not expose
    .model / .dim.  Callers stamp NULL and skip the dim guard rather than
    crash.  Centralises the three identical getattr pairs in writer +
    seed_patterns._write_pgvector*.
    """
    return getattr(embedder, "model", None), getattr(embedder, "dim", None)


def _token_split_chunk(
    base_chunk: EmbeddingChunk, content: str, start_idx: int,
) -> list[EmbeddingChunk]:
    """Split *content* by token budget, producing EmbeddingChunks starting at *start_idx*.

    Returns a single-element list when content fits within EMBEDDER_TOKEN_BUDGET
    (using start_idx as chunk_idx).  Otherwise splits and returns one chunk per
    piece with monotonically increasing chunk_idx starting at start_idx.

    Uses dataclasses.replace so all provenance fields (repo, repo_id, line_start,
    profile_name) are copied from *base_chunk* without repetition.
    """
    import dataclasses

    if estimate_tokens(content) <= EMBEDDER_TOKEN_BUDGET:
        return [dataclasses.replace(base_chunk, chunk_idx=start_idx, content=content)]
    pieces = split_by_token_budget(content, EMBEDDER_TOKEN_BUDGET)
    return [
        dataclasses.replace(base_chunk, chunk_idx=start_idx + i, content=piece)
        for i, piece in enumerate(pieces)
    ]


def _sliding(
    raw: str,
    entity_name: str,
    chunk_type: str,
    module: str,
    version: str,
    file_path: str,
    model_name: str | None,
    *,
    line_start: int | None = None,
    repo: str | None = None,
    repo_id: int | None = None,
) -> list[EmbeddingChunk]:
    """Split large content into overlapping window EmbeddingChunks.

    A3: optional keyword arguments `line_start`, `repo`, `repo_id` are
    propagated to every produced chunk (all windows share the same provenance —
    line_start points to the first line of the entity regardless of window).

    WI-B: after char-window splitting, each window is further split by
    split_by_token_budget if it exceeds EMBEDDER_TOKEN_BUDGET tokens. All
    sub-chunks from token splitting keep the same provenance.  chunk_idx is
    monotonically allocated across all windows and their token-split pieces
    so no two chunks for the same (entity_name, file_path) share an index.
    """
    # Build a prototype chunk; _token_split_chunk copies it with correct idx/content.
    _proto = EmbeddingChunk(
        chunk_type, module, version, entity_name, model_name, file_path, 0, "",
        line_start=line_start, repo=repo, repo_id=repo_id,
    )

    if len(raw) <= _WINDOW_CHARS:
        return _token_split_chunk(_proto, raw, 0)

    chunks: list[EmbeddingChunk] = []
    start = 0
    idx = 0
    while start < len(raw):
        end = min(start + _WINDOW_CHARS, len(raw))
        window = raw[start:end]
        sub_chunks = _token_split_chunk(_proto, window, idx)
        chunks.extend(sub_chunks)
        idx += len(sub_chunks)
        if end == len(raw):
            break
        start = end - _OVERLAP_CHARS

    return chunks


def _embed_chunks_resilient(
    embedder: EmbedderClient,
    chunks: list[EmbeddingChunk],
) -> tuple[list[EmbeddingChunk], list[list[float]], int]:
    """Embed all chunks, retrying the batch once on failure then degrading per-chunk.

    Happy path: embedder.embed([all contents]) in one call.
    If the batch raises (RuntimeError / any exception): retry the full batch
    once (reduces request storm for transient errors).  If the retry also
    fails: degrade to embedding chunk-by-chunk; any chunk that raises
    individually is logged as a warning and skipped.  The returned lists are
    aligned: chunks_ok[i] produced vecs[i].

    Returns:
        (chunks_ok, vecs, embed_calls)
        - chunks_ok: surviving chunks (may be shorter than input on failures)
        - vecs:      corresponding embedding vectors (same length as chunks_ok)
        - embed_calls: number of embed() calls made (for observability)
    """
    if not chunks:
        return [], [], 0

    texts = [c.content for c in chunks]
    count_before = getattr(embedder, "call_count", None)
    try:
        vecs = embedder.embed(texts)
        count_after = getattr(embedder, "call_count", None)
        if count_before is not None and count_after is not None:
            embed_calls = count_after - count_before
        else:
            embed_calls = 1
        return chunks, vecs, embed_calls
    except Exception as batch_exc:
        _logger.warning(
            "embed batch failed (%s) — retrying full batch once before per-chunk fallback",
            batch_exc,
        )

    # Retry full batch once (fix #7: reduce request storm on transient errors)
    try:
        count_before2 = getattr(embedder, "call_count", None)
        vecs = embedder.embed(texts)
        count_after2 = getattr(embedder, "call_count", None)
        if count_before2 is not None and count_after2 is not None:
            embed_calls = count_after2 - count_before2
        else:
            embed_calls = 1
        _logger.info("embed batch retry succeeded for %d chunks", len(chunks))
        return chunks, vecs, embed_calls
    except Exception as retry_exc:
        _logger.warning(
            "embed batch retry also failed (%s) — degrading to per-chunk embed; "
            "individual failures will be logged and skipped",
            retry_exc,
        )

    # Per-chunk degraded path
    ok_chunks: list[EmbeddingChunk] = []
    ok_vecs: list[list[float]] = []
    embed_calls = 0
    for c in chunks:
        try:
            [vec] = embedder.embed([c.content])
            embed_calls += 1
            ok_chunks.append(c)
            ok_vecs.append(vec)
        except Exception as chunk_exc:
            _logger.warning(
                "embed chunk skipped (module=%s entity=%s file=%s version=%s): %s",
                c.module, c.entity_name, c.file_path, c.odoo_version, chunk_exc,
            )
    return ok_chunks, ok_vecs, embed_calls


def make_chunks(
    module: str,
    version: str,
    parse_result: ParseResult,
    view_result: ViewParseResult | None,
    js_chunks: list[JSChunk] | None,
) -> list[EmbeddingChunk]:
    """Convert ParseResult + ViewParseResult + JSChunks into EmbeddingChunks.

    A3: method/field chunks carry real source file_path (from model.file_path),
    line_start (from method.line / field.line), repo and repo_id (from module).
    view/qweb chunks carry their existing real file_path plus line_start / repo /
    repo_id.  JS chunks carry repo / repo_id.  css/scss/less/pattern helpers are
    not touched here (they have no ParseResult/module context in their callers).
    """
    chunks: list[EmbeddingChunk] = []

    mod = parse_result.module
    mod_repo = mod.repo
    mod_repo_id = mod.repo_id

    for model in parse_result.models:
        # A3: use real source file_path when available; fall back to module dir.
        # ADR-0037: relativize to repo root so the stored file_path is portable
        # (idempotent — a path already relative is returned unchanged).
        model_fp = mod.relative_path(model.file_path or mod.path)

        for method in model.methods:
            prefix = f"[{module}] {model.name}.{method.name} ({version})"
            body = method.source_code or f"def {method.name}(self): ..."
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{method.name}", "method",
                module, version, model_fp, model.name,
                line_start=method.line, repo=mod_repo, repo_id=mod_repo_id,
            ))

        for fld in model.fields:
            prefix = f"[{module}] {model.name}: {fld.name} ({fld.ttype})"
            body = fld.source_definition or f"{fld.name} = fields.{fld.ttype.capitalize()}(...)"
            content = f"{prefix}\n{body}"
            chunks.extend(_sliding(
                content, f"{model.name}.{fld.name}", "field",
                module, version, model_fp, model.name,
                line_start=fld.line, repo=mod_repo, repo_id=mod_repo_id,
            ))

    if view_result:
        for view in view_result.views:
            inherit_str = f", inherit={view.inherit_xmlid}" if view.inherit_xmlid else ""
            prefix = f"[{module}] {view.xmlid} ({view.view_type}{inherit_str})"
            body = view.arch or f"<!-- arch missing for {view.xmlid} -->"
            fp = mod.relative_path(view.file_path or mod.path)
            chunks.extend(
                _sliding(
                    f"{prefix}\n{body}", view.xmlid, "view", module, version, fp, view.model,
                    line_start=view.line, repo=mod_repo, repo_id=mod_repo_id,
                )
            )

        for qweb in view_result.qweb:
            prefix = f"[{module}] {qweb.xmlid}"
            body = qweb.content or f"<!-- content missing for {qweb.xmlid} -->"
            fp = mod.relative_path(qweb.file_path or mod.path)
            chunks.extend(
                _sliding(
                    f"{prefix}\n{body}", qweb.xmlid, "qweb", module, version, fp, None,
                    line_start=qweb.line, repo=mod_repo, repo_id=mod_repo_id,
                )
            )

    for jsc in (js_chunks or []):
        chunk_type = f"js_{jsc.era}"
        content = jsc.content
        fp = mod.relative_path(jsc.file_path)
        _proto = EmbeddingChunk(
            chunk_type, module, version,
            jsc.entity_name, None, fp,
            0, "",
            repo=mod_repo, repo_id=mod_repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, content, jsc.chunk_idx))

    return chunks


def make_css_chunks(
    css_chunks: list[CSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert CSSChunk list → EmbeddingChunk list (chunk_type='css').

    Each CSSChunk (variable block, selector group, @media query, or raw window)
    becomes one EmbeddingChunk. entity_name encodes the semantic unit label
    (selector text, mixin name, variable group prefix, etc.) for ANN filtering.
    model_name is always None — CSS has no model binding.

    ADR-0037: *module_info* (when supplied) stamps repo + repo_id provenance —
    parity with method/field/view chunks so stylesheet chunks keep their repo
    identity after the file_path is relativized — and relativizes file_path to
    repo-relative form.  None → file_path verbatim, repo/repo_id NULL (back-compat).

    WI-B: each CSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in css_chunks:
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="css",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=c.entity_name,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_scss_chunks(
    scss_chunks: list[SCSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert SCSSChunk list → EmbeddingChunk list (chunk_type='scss').

    Same pattern as make_css_chunks. chunk_kind is embedded into entity_name
    as ``<kind>:<entity_name>`` so ANN results can be filtered by kind
    (e.g. find only mixin definitions across versions) without schema changes.
    model_name is always None — SCSS has no model binding.

    ADR-0037: *module_info* stamps repo + repo_id and relativizes file_path
    (see make_css_chunks).

    WI-B: each SCSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in scss_chunks:
        entity = f"{c.chunk_kind}:{c.entity_name}"
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="scss",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=entity,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_less_chunks(
    less_chunks: list[SCSSChunk], module_info: ModuleInfo | None = None,
) -> list[EmbeddingChunk]:
    """Convert SCSSChunk list (from parser_less) → EmbeddingChunk list (chunk_type='less').

    Mirrors make_scss_chunks exactly — LESS chunks share the SCSSChunk dataclass
    because the structure is identical (mixin/variable/selector/import/media/raw).
    chunk_kind is embedded into entity_name as ``<kind>:<entity_name>`` for ANN
    kind-filtering without schema changes.
    model_name is always None — LESS has no model binding.

    ADR-0037: *module_info* stamps repo + repo_id and relativizes file_path
    (see make_css_chunks).

    WI-B: each SCSSChunk content that exceeds EMBEDDER_TOKEN_BUDGET is split into
    multiple EmbeddingChunks with incrementing chunk_idx.
    """
    repo = module_info.repo if module_info else None
    repo_id = module_info.repo_id if module_info else None
    chunks: list[EmbeddingChunk] = []
    for c in less_chunks:
        entity = f"{c.chunk_kind}:{c.entity_name}"
        fp = module_info.relative_path(c.file_path) if module_info else c.file_path
        _proto = EmbeddingChunk(
            chunk_type="less",
            module=c.module,
            odoo_version=c.odoo_version,
            entity_name=entity,
            model_name=None,
            file_path=fp,
            chunk_idx=0,
            content="",
            repo=repo,
            repo_id=repo_id,
        )
        chunks.extend(_token_split_chunk(_proto, c.content, c.chunk_idx))
    return chunks


def make_pattern_chunks(patterns: list[PatternExample]) -> list[EmbeddingChunk]:
    """Convert PatternExample → EmbeddingChunk (chunk_type='pattern_example').

    Per ADR-0003 §4: language is encoded into entity_name slug as
    `<language>__<pattern_id>` so `suggest_pattern` can filter by language
    via B-tree LIKE without ALTERing the embeddings table.
    Module sentinel is `__patterns__`. odoo_version = pattern.odoo_version_min.

    WI-B: large patterns (content > EMBEDDER_TOKEN_BUDGET tokens) are split into
    multiple EmbeddingChunks with increasing chunk_idx. Each split chunk shares
    the same entity_name/file_path/module/odoo_version; unique key is
    (chunk_type, entity_name, file_path, chunk_idx).
    """
    chunks: list[EmbeddingChunk] = []
    for p in patterns:
        text_parts = [p.snippet_text]
        if p.gotchas:
            text_parts.append("---")
            text_parts.extend(p.gotchas)
        text = "\n".join(text_parts)
        entity_name = f"{p.language}__{p.pattern_id}"
        _proto = EmbeddingChunk(
            chunk_type="pattern_example",
            module="__patterns__",
            odoo_version=p.odoo_version_min,
            entity_name=entity_name,
            model_name=None,
            file_path=p.file_ref,
            chunk_idx=0,
            content="",
        )
        chunks.extend(_token_split_chunk(_proto, text, 0))
    return chunks


def write_module_embeddings(
    module: str,
    version: str,
    chunks: list[EmbeddingChunk],
    embedder: EmbedderClient,
    profile_name: str | None = None,
) -> int:
    """Delete-then-insert embeddings for (module, version[, profile_name]) atomically.

    profile_name scopes the delete to a single tenant's chunks so re-indexing
    profile A does not erase profile B's chunks for the same module/version.
    NULL (default) is the shared/global scope used for pattern chunks and for
    legacy callers that pre-date multi-tenant support.

    Obtains a pgvector-capable connection from the shared pool (get_pool().checkout_vec()).
    Returns the number of embed() calls made to the embedder during this
    write (0 when chunks is empty, 1 for a normal module batch). Callers
    use this to aggregate embed_calls for the run-level observability log.

    WI-B: uses _embed_chunks_resilient to degrade gracefully on partial failures;
    writes embedding_model and embedding_dim into each row; calls assert_dim_matches
    once per batch as fail-fast guard against incompatible vector spaces.
    """
    if not chunks:
        return 0
    # Stamp every chunk with the profile_name supplied at write time.
    # This is cleaner than threading profile_name through every make_*_chunks
    # helper: the helpers remain profile-agnostic; the write call is the single
    # authoritative place that knows which profile owns these chunks.
    for c in chunks:
        c.profile_name = profile_name
    # Dedup by the ux_embeddings_chunk unique key — make_module_chunks can emit
    # the same (chunk_type, entity_name, file_path, chunk_idx) twice for one
    # module (e.g. partial classes split across files). Postgres rejects a
    # single INSERT batch containing such duplicates with `ON CONFLICT DO UPDATE
    # command cannot affect row a second time` even when the ON CONFLICT clause
    # would otherwise resolve them across separate statements. Last-wins.
    seen: dict[tuple, EmbeddingChunk] = {}
    for c in chunks:
        seen[(c.chunk_type, c.entity_name, c.file_path, c.chunk_idx)] = c
    chunks = list(seen.values())

    live_chunks, vecs, embed_calls = _embed_chunks_resilient(embedder, chunks)

    # Guard: if all chunks failed embedding (total failure), do NOT delete existing
    # rows and insert nothing — that would silently wipe the module's embeddings.
    # Preserve the existing rows and let the caller retry later.
    if not live_chunks:
        _logger.error(
            "embed produced 0 usable vectors for module=%s version=%s — "
            "preserving existing rows, skipping destructive rewrite",
            module, version,
        )
        return embed_calls

    # Embedders that pre-date the provider-abstraction (ADR-0045) — and some test
    # doubles — may not expose .model/.dim. Tolerate their absence: stamp NULL
    # (the columns are nullable) and skip the dim guard rather than crash.
    emb_model, emb_dim = _embedder_meta(embedder)

    from src.db.pg import get_pool  # noqa: PLC0415
    with get_pool().checkout_vec() as conn:
        # Fail-fast guard: raises EmbedderDimMismatch if configured dim != stored dim.
        # Pass emb_model so R5's extended guard can also detect model-switch with
        # same dim (assert_dim_matches signature extended per R5 cross-contract).
        if emb_dim is not None:
            from src.db.embedding_guard import assert_dim_matches  # noqa: PLC0415
            assert_dim_matches(conn, emb_dim, emb_model)

        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM embeddings "
                    "WHERE module = %s AND odoo_version = %s "
                    "AND profile_name IS NOT DISTINCT FROM %s",
                    (module, version, profile_name),
                )
                rows = [
                    c.as_tuple(vecs[i], emb_model, emb_dim)
                    for i, c in enumerate(live_chunks)
                ]
                execute_values(cur, _INSERT_SQL, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True  # restore for pool reuse
    return embed_calls
